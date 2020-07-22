import numpy as np
from typing import Dict, List, Mapping, cast, Tuple
import torch
from torch import nn

from mlagents_envs.logging_util import get_logger
from mlagents.trainers.optimizer.torch_optimizer import TorchOptimizer
from mlagents.trainers.policy.torch_policy import TorchPolicy
from mlagents.trainers.settings import NetworkSettings
from mlagents.trainers.brain import CameraResolution
from mlagents.trainers.models_torch import (
    Critic,
    QNetwork,
    ActionType,
    list_to_tensor,
    break_into_branches,
    actions_to_onehot,
)
from mlagents.trainers.buffer import AgentBuffer
from mlagents_envs.timers import timed
from mlagents.trainers.exception import UnityTrainerException
from mlagents.trainers.settings import TrainerSettings, SACSettings

EPSILON = 1e-6  # Small value to avoid divide by zero

logger = get_logger(__name__)


class TorchSACOptimizer(TorchOptimizer):
    class PolicyValueNetwork(nn.Module):
        def __init__(
            self,
            stream_names: List[str],
            vector_sizes: List[int],
            visual_sizes: List[CameraResolution],
            network_settings: NetworkSettings,
            act_type: ActionType,
            act_size: List[int],
        ):
            super().__init__()
            self.q1_network = QNetwork(
                stream_names,
                vector_sizes,
                visual_sizes,
                network_settings,
                act_type,
                act_size,
            )
            self.q2_network = QNetwork(
                stream_names,
                vector_sizes,
                visual_sizes,
                network_settings,
                act_type,
                act_size,
            )

        def forward(
            self,
            vec_inputs: List[torch.Tensor],
            vis_inputs: List[torch.Tensor],
            actions: torch.Tensor = None,
        ) -> Tuple[Dict[str, torch.Tensor], Dict[str, torch.Tensor]]:
            q1_out, _ = self.q1_network(vec_inputs, vis_inputs, actions=actions)
            q2_out, _ = self.q2_network(vec_inputs, vis_inputs, actions=actions)
            return q1_out, q2_out

    def __init__(self, policy: TorchPolicy, trainer_params: TrainerSettings):
        super().__init__(policy, trainer_params)
        hyperparameters: SACSettings = cast(SACSettings, trainer_params.hyperparameters)
        lr = hyperparameters.learning_rate
        # lr_schedule = hyperparameters.learning_rate_schedule
        # max_step = trainer_params.max_steps
        self.tau = hyperparameters.tau
        self.init_entcoef = hyperparameters.init_entcoef

        self.policy = policy
        self.act_size = policy.act_size
        policy_network_settings = policy.network_settings
        # h_size = policy_network_settings.hidden_units
        # num_layers = policy_network_settings.num_layers
        # vis_encode_type = policy_network_settings.vis_encode_type

        self.tau = hyperparameters.tau
        self.burn_in_ratio = 0.0

        # Non-exposed SAC parameters
        self.discrete_target_entropy_scale = 0.2  # Roughly equal to e-greedy 0.05
        self.continuous_target_entropy_scale = 1.0

        self.stream_names = list(self.reward_signals.keys())
        # Use to reduce "survivor bonus" when using Curiosity or GAIL.
        self.gammas = [_val.gamma for _val in trainer_params.reward_signals.values()]
        self.use_dones_in_backup = {
            name: int(self.reward_signals[name].use_terminal_states)
            for name in self.stream_names
        }
        # self.disable_use_dones = {
        #     name: self.use_dones_in_backup[name].assign(0.0)
        #     for name in stream_names
        # }

        brain = policy.brain
        self.value_network = TorchSACOptimizer.PolicyValueNetwork(
            self.stream_names,
            [brain.vector_observation_space_size],
            brain.camera_resolutions,
            policy_network_settings,
            ActionType.from_str(policy.act_type),
            self.act_size,
        )
        self.target_network = Critic(
            self.stream_names,
            policy_network_settings.hidden_units,
            [brain.vector_observation_space_size],
            brain.camera_resolutions,
            policy_network_settings.normalize,
            policy_network_settings.num_layers,
            policy_network_settings.memory.memory_size
            if policy_network_settings.memory is not None
            else 0,
            policy_network_settings.vis_encode_type,
        )
        self.soft_update(self.policy.actor_critic.critic, self.target_network, 1.0)

        self._log_ent_coef = torch.nn.Parameter(
            torch.log(torch.as_tensor([self.init_entcoef] * len(self.act_size))),
            requires_grad=True,
        )
        if self.policy.use_continuous_act:
            self.target_entropy = torch.as_tensor(
                -1
                * self.continuous_target_entropy_scale
                * np.prod(self.act_size[0]).astype(np.float32)
            )
        else:
            self.target_entropy = [
                self.discrete_target_entropy_scale * np.log(i).astype(np.float32)
                for i in self.act_size
            ]

        policy_params = list(self.policy.actor_critic.network_body.parameters()) + list(
            self.policy.actor_critic.distribution.parameters()
        )
        value_params = list(self.value_network.parameters()) + list(
            self.policy.actor_critic.critic.parameters()
        )

        logger.debug("value_vars")
        for param in value_params:
            logger.debug(param.shape)
        logger.debug("policy_vars")
        for param in policy_params:
            logger.debug(param.shape)

        self.policy_optimizer = torch.optim.Adam(policy_params, lr=lr)
        self.value_optimizer = torch.optim.Adam(value_params, lr=lr)
        self.entropy_optimizer = torch.optim.Adam([self._log_ent_coef], lr=lr)

    def sac_q_loss(
        self,
        q1_out: Dict[str, torch.Tensor],
        q2_out: Dict[str, torch.Tensor],
        target_values: Dict[str, torch.Tensor],
        dones: torch.Tensor,
        rewards: Dict[str, torch.Tensor],
        loss_masks: torch.Tensor,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        q1_losses = []
        q2_losses = []
        # Multiple q losses per stream
        for i, name in enumerate(q1_out.keys()):
            q1_stream = q1_out[name].squeeze()
            q2_stream = q2_out[name].squeeze()
            with torch.no_grad():
                q_backup = rewards[name] + (
                    (1.0 - self.use_dones_in_backup[name] * dones)
                    * self.gammas[i]
                    * target_values[name]
                )
            _q1_loss = 0.5 * torch.mean(
                loss_masks * torch.nn.functional.mse_loss(q_backup, q1_stream)
            )
            _q2_loss = 0.5 * torch.mean(
                loss_masks * torch.nn.functional.mse_loss(q_backup, q2_stream)
            )

            q1_losses.append(_q1_loss)
            q2_losses.append(_q2_loss)
        q1_loss = torch.mean(torch.stack(q1_losses))
        q2_loss = torch.mean(torch.stack(q2_losses))
        return q1_loss, q2_loss

    def soft_update(self, source: nn.Module, target: nn.Module, tau: float) -> None:
        for source_param, target_param in zip(source.parameters(), target.parameters()):
            target_param.data.copy_(
                target_param.data * (1.0 - tau) + source_param.data * tau
            )

    def sac_value_loss(
        self,
        log_probs: torch.Tensor,
        values: Dict[str, torch.Tensor],
        q1p_out: Dict[str, torch.Tensor],
        q2p_out: Dict[str, torch.Tensor],
        loss_masks: torch.Tensor,
        discrete: bool,
    ) -> torch.Tensor:
        min_policy_qs = {}
        with torch.no_grad():
            _ent_coef = torch.exp(self._log_ent_coef)
        for name in values.keys():
            if not discrete:
                min_policy_qs[name] = torch.min(q1p_out[name], q2p_out[name])
            else:
                action_probs = log_probs.exp()
                _branched_q1p = break_into_branches(
                    q1p_out[name] * action_probs, self.act_size
                )
                _branched_q2p = break_into_branches(
                    q2p_out[name] * action_probs, self.act_size
                )
                _q1p_mean = torch.mean(
                    torch.stack(
                        [torch.sum(_br, dim=1, keepdim=True) for _br in _branched_q1p]
                    ),
                    dim=0,
                )
                _q2p_mean = torch.mean(
                    torch.stack(
                        [torch.sum(_br, dim=1, keepdim=True) for _br in _branched_q2p]
                    ),
                    dim=0,
                )

                min_policy_qs[name] = torch.min(_q1p_mean, _q2p_mean)

        value_losses = []
        if not discrete:
            for name in values.keys():
                with torch.no_grad():
                    v_backup = min_policy_qs[name] - torch.sum(
                        _ent_coef * log_probs, dim=1
                    )
                # print(log_probs, v_backup, _ent_coef, loss_masks)
                value_loss = 0.5 * torch.mean(
                    loss_masks * torch.nn.functional.mse_loss(values[name], v_backup)
                )
                value_losses.append(value_loss)
        else:
            branched_per_action_ent = break_into_branches(
                log_probs * log_probs.exp(), self.act_size
            )
            # We have to do entropy bonus per action branch
            branched_ent_bonus = torch.stack(
                [
                    torch.sum(_ent_coef[i] * _lp, dim=1, keepdim=True)
                    for i, _lp in enumerate(branched_per_action_ent)
                ]
            )
            for name in values.keys():
                with torch.no_grad():
                    v_backup = min_policy_qs[name] - torch.mean(
                        branched_ent_bonus, axis=0
                    )
                value_loss = 0.5 * torch.mean(
                    loss_masks
                    * torch.nn.functional.mse_loss(values[name], v_backup.squeeze())
                )
                value_losses.append(value_loss)
        value_loss = torch.mean(torch.stack(value_losses))
        if torch.isinf(value_loss).any() or torch.isnan(value_loss).any():
            raise UnityTrainerException("Inf found")
        return value_loss

    def sac_policy_loss(
        self,
        log_probs: torch.Tensor,
        q1p_outs: Dict[str, torch.Tensor],
        loss_masks: torch.Tensor,
        discrete: bool,
    ) -> torch.Tensor:
        _ent_coef = torch.exp(self._log_ent_coef)
        mean_q1 = torch.mean(torch.stack(list(q1p_outs.values())), axis=0)
        if not discrete:
            mean_q1 = mean_q1.unsqueeze(1)
            batch_policy_loss = torch.mean(_ent_coef * log_probs - mean_q1, dim=1)
            policy_loss = torch.mean(loss_masks * batch_policy_loss)
        else:
            action_probs = log_probs.exp()
            branched_per_action_ent = break_into_branches(
                log_probs * action_probs, self.act_size
            )
            branched_q_term = break_into_branches(mean_q1 * action_probs, self.act_size)
            branched_policy_loss = torch.stack(
                [
                    torch.sum(_ent_coef[i] * _lp - _qt, dim=1, keepdim=True)
                    for i, (_lp, _qt) in enumerate(
                        zip(branched_per_action_ent, branched_q_term)
                    )
                ]
            )
            batch_policy_loss = torch.squeeze(branched_policy_loss)
        policy_loss = torch.mean(loss_masks * batch_policy_loss)
        return policy_loss

    def sac_entropy_loss(
        self, log_probs: torch.Tensor, loss_masks: torch.Tensor, discrete: bool
    ) -> torch.Tensor:
        if not discrete:
            with torch.no_grad():
                target_current_diff = torch.sum(log_probs + self.target_entropy, dim=1)
            entropy_loss = -torch.mean(
                self._log_ent_coef * loss_masks * target_current_diff
            )
        else:
            with torch.no_grad():
                branched_per_action_ent = break_into_branches(
                    log_probs * log_probs.exp(), self.act_size
                )
                target_current_diff_branched = torch.stack(
                    [
                        torch.sum(_lp, axis=1, keepdim=True) + _te
                        for _lp, _te in zip(
                            branched_per_action_ent, self.target_entropy
                        )
                    ],
                    axis=1,
                )
                target_current_diff = torch.squeeze(
                    target_current_diff_branched, axis=2
                )
            entropy_loss = -torch.mean(
                loss_masks
                * torch.mean(self._log_ent_coef * target_current_diff, axis=1)
            )

        return entropy_loss

    def _condense_q_streams(
        self, q_output: Dict[str, torch.Tensor], discrete_actions: torch.Tensor
    ) -> Dict[str, torch.Tensor]:
        condensed_q_output = {}
        onehot_actions = actions_to_onehot(discrete_actions, self.act_size)
        for key, item in q_output.items():
            branched_q = break_into_branches(item, self.act_size)
            only_action_qs = torch.stack(
                [
                    torch.sum(_act * _q, dim=1, keepdim=True)
                    for _act, _q in zip(onehot_actions, branched_q)
                ]
            )

            condensed_q_output[key] = torch.mean(only_action_qs, dim=0)
        return condensed_q_output

    @timed
    def update(self, batch: AgentBuffer, num_sequences: int) -> Dict[str, float]:
        """
        Updates model using buffer.
        :param num_sequences: Number of trajectories in batch.
        :param batch: Experience mini-batch.
        :param update_target: Whether or not to update target value network
        :param reward_signal_batches: Minibatches to use for updating the reward signals,
            indexed by name. If none, don't update the reward signals.
        :return: Output from update process.
        """
        rewards = {}
        for name in self.reward_signals:
            rewards[name] = list_to_tensor(batch["{}_rewards".format(name)])

        vec_obs = [list_to_tensor(batch["vector_obs"])]
        next_vec_obs = [list_to_tensor(batch["next_vector_in"])]
        act_masks = list_to_tensor(batch["action_mask"])
        if self.policy.use_continuous_act:
            actions = list_to_tensor(batch["actions"]).unsqueeze(-1)
        else:
            actions = list_to_tensor(batch["actions"], dtype=torch.long)

        memories = [
            list_to_tensor(batch["memory"][i])
            for i in range(0, len(batch["memory"]), self.policy.sequence_length)
        ]
        if len(memories) > 0:
            memories = torch.stack(memories).unsqueeze(0)
        vis_obs: List[torch.Tensor] = []
        next_vis_obs: List[torch.Tensor] = []
        if self.policy.use_vis_obs:
            vis_obs = []
            for idx, _ in enumerate(
                self.policy.actor_critic.network_body.visual_encoders
            ):
                vis_ob = list_to_tensor(batch["visual_obs%d" % idx])
                vis_obs.append(vis_ob)
                next_vis_ob = list_to_tensor(batch["next_visual_obs%d" % idx])
                next_vis_obs.append(next_vis_ob)

        # Copy normalizers from policy
        self.value_network.q1_network.copy_normalization(
            self.policy.actor_critic.network_body
        )
        self.value_network.q2_network.copy_normalization(
            self.policy.actor_critic.network_body
        )
        self.target_network.network_body.copy_normalization(
            self.policy.actor_critic.network_body
        )
        (
            sampled_actions,
            log_probs,
            entropies,
            sampled_values,
            _,
        ) = self.policy.sample_actions(
            vec_obs,
            vis_obs,
            masks=act_masks,
            memories=memories,
            seq_len=self.policy.sequence_length,
            all_log_probs=not self.policy.use_continuous_act,
        )
        if self.policy.use_continuous_act:
            squeezed_actions = actions.squeeze(-1)
            q1p_out, q2p_out = self.value_network(vec_obs, vis_obs, sampled_actions)
            q1_out, q2_out = self.value_network(vec_obs, vis_obs, squeezed_actions)
            q1_stream, q2_stream = q1_out, q2_out
        else:
            with torch.no_grad():
                q1p_out, q2p_out = self.value_network(vec_obs, vis_obs)
            q1_out, q2_out = self.value_network(vec_obs, vis_obs)
            q1_stream = self._condense_q_streams(q1_out, actions)
            q2_stream = self._condense_q_streams(q2_out, actions)

        with torch.no_grad():
            target_values, _ = self.target_network(next_vec_obs, next_vis_obs)
        masks = list_to_tensor(batch["masks"], dtype=torch.int32)
        use_discrete = not self.policy.use_continuous_act
        dones = list_to_tensor(batch["done"])

        q1_loss, q2_loss = self.sac_q_loss(
            q1_stream, q2_stream, target_values, dones, rewards, masks
        )
        value_loss = self.sac_value_loss(
            log_probs, sampled_values, q1p_out, q2p_out, masks, use_discrete
        )
        policy_loss = self.sac_policy_loss(log_probs, q1p_out, masks, use_discrete)
        entropy_loss = self.sac_entropy_loss(log_probs, masks, use_discrete)

        total_value_loss = q1_loss + q2_loss + value_loss

        self.policy_optimizer.zero_grad()
        policy_loss.backward()
        self.policy_optimizer.step()

        self.value_optimizer.zero_grad()
        total_value_loss.backward()
        self.value_optimizer.step()

        self.entropy_optimizer.zero_grad()
        entropy_loss.backward()
        self.entropy_optimizer.step()

        # Update target network
        self.soft_update(self.policy.actor_critic.critic, self.target_network, self.tau)
        update_stats = {
            "Losses/Policy Loss": abs(policy_loss.detach().cpu().numpy()),
            "Losses/Value Loss": value_loss.detach().cpu().numpy(),
            "Losses/Q1 Loss": q1_loss.detach().cpu().numpy(),
            "Losses/Q2 Loss": q2_loss.detach().cpu().numpy(),
            "Policy/Entropy Coeff": torch.exp(self._log_ent_coef)
            .detach()
            .cpu()
            .numpy(),
        }

        return update_stats

    def update_reward_signals(
        self, reward_signal_minibatches: Mapping[str, AgentBuffer], num_sequences: int
    ) -> Dict[str, float]:
        return {}
