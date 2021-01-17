"""
Implement soft actor critic agent here.
1. Full pipeline running
2. Restart from behavior policy
3. Restart from Q_b
"""

import os
import time

import gym
import numpy as np
import tensorflow as tf
import tensorflow_probability as tfp
from rlutils.future.optimizer import get_adam_optimizer, minimize
from rlutils.generative_models.vae import EnsembleBehaviorPolicy
from rlutils.logx import EpochLogger
from rlutils.replay_buffers import PyUniformParallelEnvReplayBuffer
from rlutils.runner import TFRunner
from rlutils.tf.distributions import apply_squash_log_prob
from rlutils.tf.functional import soft_update, hard_update, to_numpy_or_python_type
from rlutils.tf.nn import SquashedGaussianMLPActor, EnsembleMinQNet, LagrangeLayer, CenteredBetaMLPActor
from tqdm.auto import tqdm, trange

tfd = tfp.distributions

__all__ = ['SquashedGaussianMLPActor', 'CenteredBetaMLPActor']


class BRACPAgent(tf.keras.Model):
    def __init__(self,
                 ob_dim,
                 ac_dim,
                 num_ensembles=5,
                 behavior_mlp_hidden=256,
                 behavior_lr=1e-4,
                 policy_mlp_hidden=128,
                 q_mlp_hidden=256,
                 q_lr=3e-4,
                 alpha_lr=1e-3,
                 alpha=1.0,
                 tau=5e-3,
                 gamma=0.99,
                 target_entropy=None,
                 use_gp=True,
                 reg_type='kl',
                 sigma=10,
                 n=5,
                 gp_weight=0.1,
                 entropy_reg=True,
                 kl_backup=False,
                 ):
        super(BRACPAgent, self).__init__()
        self.reg_type = reg_type
        assert self.reg_type in ['kl', 'mmd', 'cross_entropy']

        self.ob_dim = ob_dim
        self.ac_dim = ac_dim
        self.q_mlp_hidden = q_mlp_hidden
        self.behavior_policy = EnsembleBehaviorPolicy(num_ensembles=num_ensembles, out_dist='normal',
                                                      obs_dim=self.ob_dim, act_dim=self.ac_dim,
                                                      mlp_hidden=behavior_mlp_hidden)
        self.behavior_lr = behavior_lr
        self.policy_net = SquashedGaussianMLPActor(ob_dim, ac_dim, policy_mlp_hidden)
        self.target_policy_net = SquashedGaussianMLPActor(ob_dim, ac_dim, policy_mlp_hidden)
        hard_update(self.target_policy_net, self.policy_net)
        self.q_network = EnsembleMinQNet(ob_dim, ac_dim, q_mlp_hidden)
        self.q_network.compile(optimizer=get_adam_optimizer(q_lr))
        self.target_q_network = EnsembleMinQNet(ob_dim, ac_dim, q_mlp_hidden)
        hard_update(self.target_q_network, self.q_network)

        self.log_beta = LagrangeLayer(initial_value=alpha)
        self.log_beta.compile(optimizer=get_adam_optimizer(alpha_lr))

        self.log_alpha = LagrangeLayer(initial_value=alpha)
        self.log_alpha.compile(optimizer=get_adam_optimizer(alpha_lr))

        self.log_gp = LagrangeLayer(initial_value=gp_weight)
        self.log_gp.compile(optimizer=get_adam_optimizer(alpha_lr))

        self.target_entropy = target_entropy

        self.tau = tau
        self.gamma = gamma

        self.kl_n = 5
        self.n = n
        self.max_q_backup = True
        self.entropy_reg = entropy_reg
        self.kl_backup = kl_backup
        self.gradient_clipping = False
        self.sensitivity = 1.0
        self.max_ood_grad_norm = 0.01
        self.use_gp = use_gp
        self.sigma = sigma

        # delta should set according to the KL between initial policy and behavior policy
        self.delta_behavior = tf.Variable(initial_value=0.0, trainable=False, dtype=tf.float32)

    def get_alpha(self, obs):
        return self.log_alpha(obs)

    def call(self, inputs, training=None, mask=None):
        obs, deterministic = inputs
        pi_final = self.policy_net((obs, deterministic))[0]
        return pi_final

    def set_delta_behavior(self, delta_behavior):
        EpochLogger.log(f'Setting behavior hard KL to {delta_behavior:.4f}')
        self.delta_behavior.assign(delta_behavior)

    def set_logger(self, logger):
        self.logger = logger

    def log_tabular(self):
        self.logger.log_tabular('Q1Vals', with_min_and_max=True)
        self.logger.log_tabular('Q2Vals', with_min_and_max=True)
        self.logger.log_tabular('LogPi', average_only=True)
        self.logger.log_tabular('LossPi', average_only=True)
        self.logger.log_tabular('LossQ', average_only=True)
        self.logger.log_tabular('Alpha', average_only=True)
        self.logger.log_tabular('LossAlpha', average_only=True)

        self.logger.log_tabular('KL', with_min_and_max=True)
        self.logger.log_tabular('ViolationRatio', average_only=True)
        self.logger.log_tabular('Beta', average_only=True)
        self.logger.log_tabular('BetaLoss', average_only=True)
        self.logger.log_tabular('BehaviorLoss', average_only=True)
        self.logger.log_tabular('GP', average_only=True)
        self.logger.log_tabular('GPWeight', average_only=True)

    @tf.function
    def update_target(self):
        soft_update(self.target_q_network, self.q_network, self.tau)
        soft_update(self.target_policy_net, self.policy_net, self.tau)

    @tf.function(experimental_relax_shapes=True)
    def compute_pi_pib_distance(self, obs):
        if self.reg_type in ['kl', 'cross_entropy']:
            _, log_prob, raw_action, pi_distribution = self.policy_net((obs, False))
            loss = self._compute_kl_behavior_v2(obs, raw_action, pi_distribution)
        elif self.reg_type == 'mmd':
            batch_size = tf.shape(obs)[0]
            obs = tf.tile(obs, (self.n, 1))
            _, log_prob, raw_action, pi_distribution = self.policy_net((obs, False))
            loss = self._compute_mmd(obs, raw_action, pi_distribution)
            log_prob = tf.reduce_mean(tf.reshape(log_prob, shape=(self.n, batch_size)), axis=0)
        else:
            raise NotImplementedError
        return loss, log_prob

    def mmd_loss_laplacian(self, samples1, samples2, sigma=0.2):
        """MMD constraint with Laplacian kernel for support matching"""
        # sigma is set to 10.0 for hopper, cheetah and 20 for walker/ant
        # (n, None, ac_dim)
        diff_x_x = tf.expand_dims(samples1, axis=0) - tf.expand_dims(samples1, axis=1)  # (n, n, None, ac_dim)
        diff_x_x = tf.reduce_mean(tf.exp(-tf.reduce_sum(tf.abs(diff_x_x), axis=-1) / (2.0 * sigma)), axis=(0, 1))

        diff_x_y = tf.expand_dims(samples1, axis=0) - tf.expand_dims(samples2, axis=1)
        diff_x_y = tf.reduce_mean(tf.exp(-tf.reduce_sum(tf.abs(diff_x_y), axis=-1) / (2.0 * sigma)), axis=(0, 1))

        diff_y_y = tf.expand_dims(samples2, axis=0) - tf.expand_dims(samples2, axis=1)  # (n, n, None, ac_dim)
        diff_y_y = tf.reduce_mean(tf.exp(-tf.reduce_sum(tf.abs(diff_y_y), axis=-1) / (2.0 * sigma)), axis=(0, 1))
        overall_loss = tf.sqrt(diff_x_x + diff_y_y - 2.0 * diff_x_y + 1e-6)  # (None,)
        return overall_loss

    def _compute_mmd(self, obs, raw_action, pi_distribution):
        # obs: (n * None, obs_dim), raw_actions: (n * None, ac_dim)
        batch_size = tf.shape(obs)[0] // self.n
        samples_pi = raw_action
        samples_pi = tf.tile(samples_pi, (self.behavior_policy.num_ensembles, 1))
        samples_pi = tf.reshape(samples_pi, shape=(self.behavior_policy.num_ensembles, self.n,
                                                   batch_size, self.ac_dim))
        samples_pi = tf.transpose(samples_pi, perm=[1, 0, 2, 3])
        samples_pi = tf.reshape(samples_pi, shape=(self.n, self.behavior_policy.num_ensembles * batch_size,
                                                   self.ac_dim))

        obs_expand = self.behavior_policy.expand_ensemble_dim(obs)
        samples_pi_b = self.behavior_policy.sample(
            obs_expand, full_path=tf.convert_to_tensor(True))  # (num_ensembles, n * batch_size, d)
        samples_pi_b = tf.reshape(samples_pi_b, shape=(self.behavior_policy.num_ensembles, self.n,
                                                       batch_size, self.ac_dim))
        samples_pi_b = tf.transpose(samples_pi_b, perm=[1, 0, 2, 3])
        samples_pi_b = tf.reshape(samples_pi_b, shape=(self.n, self.behavior_policy.num_ensembles * batch_size,
                                                       self.ac_dim))
        samples_pi = self.policy_net.transform_raw_action(samples_pi)
        samples_pi_b = self.behavior_policy.transform_raw_action(samples_pi_b)
        mmd_loss = self.mmd_loss_laplacian(samples_pi, samples_pi_b, sigma=self.sigma)
        mmd_loss = tf.reshape(mmd_loss, shape=(self.behavior_policy.num_ensembles, batch_size))
        mmd_loss = tf.reduce_mean(mmd_loss, axis=0)
        return mmd_loss

    def _compute_kl_behavior_v2(self, obs, raw_action, pi_distribution):
        n = self.kl_n
        batch_size = tf.shape(obs)[0]
        pi_distribution = tfd.Independent(distribution=tfd.Normal(
            loc=tf.tile(pi_distribution.distribution.loc, (n, 1)),
            scale=tf.tile(pi_distribution.distribution.scale, (n, 1))
        ), reinterpreted_batch_ndims=1)  # (n * batch_size)
        # compute KLD upper bound
        x, cond = raw_action, obs
        print(f'Tracing call_n with x={x}, cond={cond}')
        x = self.behavior_policy.expand_ensemble_dim(x)  # (num_ensembles, None, act_dim)
        cond = self.behavior_policy.expand_ensemble_dim(cond)  # (num_ensembles, None, obs_dim)
        posterior = self.behavior_policy.encode_distribution(inputs=(x, cond))
        encode_sample = posterior.sample(n)  # (n, num_ensembles, None, z_dim)
        encode_sample = tf.transpose(encode_sample, perm=[1, 0, 2, 3])  # (num_ensembles, n, None, z_dim)
        encode_sample = tf.reshape(encode_sample, shape=(self.behavior_policy.num_ensembles,
                                                         n * batch_size,
                                                         self.behavior_policy.latent_dim))
        cond = tf.tile(cond, multiples=(1, n, 1))  # (num_ensembles, n * None, obs_dim)
        beta_distribution = self.behavior_policy.decode_distribution(z=(encode_sample, cond))  # (ensemble, n * None)
        posterior_kld = tfd.kl_divergence(posterior, self.behavior_policy.prior)  # (num_ensembles, None,)
        posterior_kld = tf.tile(posterior_kld, multiples=(1, n,))

        if self.reg_type == 'kl':
            kl_loss = tfd.kl_divergence(pi_distribution, beta_distribution)  # (ensembles, n * None)
        elif self.reg_type == 'cross_entropy':
            # Cross entropy
            x = tf.tile(x, multiples=(1, n, 1))  # (num_ensembles, n * None, act_dim)
            kl_loss = beta_distribution.log_prob(x)  # (ensembles, None * n)
            kl_loss = -apply_squash_log_prob(kl_loss, x)
        else:
            raise NotImplementedError

        final_kl_loss = kl_loss + posterior_kld  # (ensembles, None * n)
        final_kl_loss = tf.reshape(final_kl_loss, shape=(self.behavior_policy.num_ensembles, n, batch_size))
        final_kl_loss = tf.reduce_mean(final_kl_loss, axis=[0, 1])  # average both latent and ensemble dimension
        return final_kl_loss

    @tf.function
    def update_actor_first_order(self, obs):
        # TODO: maybe we just follow behavior policy and keep a minimum entropy instead of the optimal one.
        # policy loss
        with tf.GradientTape() as policy_tape:
            """ Compute the loss function of the policy that maximizes the Q function """
            print(f'Tracing _compute_surrogate_loss_pi with obs={obs}')

            policy_tape.watch(self.policy_net.trainable_variables)

            batch_size = tf.shape(obs)[0]
            alpha = self.get_alpha(obs)  # (None, act_dim)
            beta = self.log_beta(obs)

            obs_tile = tf.tile(obs, (self.n, 1))

            # policy loss
            action, log_prob, raw_action, pi_distribution = self.policy_net((obs_tile, False))
            log_prob = tf.reduce_mean(tf.reshape(log_prob, shape=(self.n, batch_size)), axis=0)
            q_values_pi_min = self.q_network((obs_tile, action), training=False)
            q_values_pi_min = tf.reduce_mean(tf.reshape(q_values_pi_min, shape=(self.n, batch_size)), axis=0)
            # add KL divergence penalty, high variance?
            if self.reg_type in ['kl', 'cross_entropy']:
                kl_loss = self._compute_kl_behavior_v2(obs_tile, raw_action, pi_distribution)  # (None, act_dim)
                kl_loss = tf.reduce_mean(tf.reshape(kl_loss, shape=(self.n, batch_size)), axis=0)
            elif self.reg_type == 'mmd':
                kl_loss = self._compute_mmd(obs_tile, raw_action, pi_distribution)
            else:
                raise NotImplementedError

            delta = kl_loss - self.delta_behavior
            penalty = delta * alpha  # (None, act_dim)

            if self.reg_type == 'kl':
                if self.entropy_reg:
                    policy_loss = tf.reduce_mean(- q_values_pi_min + penalty - beta * log_prob, axis=0)
                else:
                    policy_loss = tf.reduce_mean(- q_values_pi_min + penalty, axis=0)
            elif self.reg_type in ['mmd', 'cross_entropy']:
                if self.entropy_reg:
                    policy_loss = tf.reduce_mean(- q_values_pi_min + penalty + beta * log_prob, axis=0)
                else:
                    policy_loss = tf.reduce_mean(- q_values_pi_min + penalty, axis=0)
            else:
                raise NotImplementedError

        minimize(policy_loss, policy_tape, self.policy_net)

        if self.entropy_reg:
            with tf.GradientTape() as beta_tape:
                beta_tape.watch(self.log_beta.trainable_variables)
                beta = self.log_beta(obs)
                # beta loss
                if self.reg_type == 'kl':
                    beta_loss = tf.reduce_mean(beta * (log_prob + self.target_entropy))
                elif self.reg_type in ['mmd', 'cross_entropy']:
                    beta_loss = -tf.reduce_mean(beta * (log_prob + self.target_entropy))
                else:
                    raise NotImplementedError
            minimize(beta_loss, beta_tape, self.log_beta)
        else:
            beta_loss = 0.

        with tf.GradientTape() as alpha_tape:
            # alpha loss
            alpha = self.get_alpha(obs)
            penalty = delta * alpha
            alpha_loss = -tf.reduce_mean(penalty, axis=0)

        minimize(alpha_loss, alpha_tape, self.log_alpha)

        info = dict(
            LossPi=policy_loss,
            KL=kl_loss,
            ViolationRatio=tf.reduce_mean(tf.cast(delta > 0., dtype=tf.float32), axis=-1),
            Alpha=alpha,
            LossAlpha=alpha_loss,
            Beta=beta,
            BetaLoss=beta_loss,
            LogPi=log_prob,
        )

        return info

    @tf.function
    def update_actor_cloning(self, obs):
        """ Minimize KL(pi, pi_b) """
        with tf.GradientTape() as policy_tape:
            policy_tape.watch(self.policy_net.trainable_variables)
            beta = self.log_beta(obs)
            loss, log_prob = self.compute_pi_pib_distance(obs)
            if self.entropy_reg:
                if self.reg_type in ['kl']:
                    policy_loss = tf.reduce_mean(loss - beta * log_prob, axis=0)
                elif self.reg_type in ['mmd', 'cross_entropy']:
                    policy_loss = tf.reduce_mean(loss + beta * log_prob, axis=0)
                else:
                    raise NotImplementedError
            else:
                policy_loss = tf.reduce_mean(loss, axis=0)

        minimize(policy_loss, policy_tape, self.policy_net)

        if self.entropy_reg:
            with tf.GradientTape() as beta_tape:
                beta_tape.watch(self.log_beta.trainable_variables)
                beta = self.log_beta(obs)
                if self.reg_type in ['kl']:
                    beta_loss = tf.reduce_mean(beta * (log_prob + self.target_entropy), axis=0)
                elif self.reg_type in ['mmd', 'cross_entropy']:
                    beta_loss = -tf.reduce_mean(beta * (log_prob + self.target_entropy), axis=0)
                else:
                    raise NotImplementedError
            minimize(beta_loss, beta_tape, self.log_beta)

        info = dict(
            KL=loss,
            LogPi=log_prob,
        )
        return info

    def _compute_target_q(self, next_obs, reward, done):
        batch_size = tf.shape(next_obs)[0]
        alpha = self.get_alpha(next_obs)
        if self.max_q_backup is True:
            next_obs = tf.tile(next_obs, multiples=(self.n, 1))
        next_action, next_action_log_prob, next_raw_action, pi_distribution = self.target_policy_net((next_obs, False))
        target_q_values = self.target_q_network((next_obs, next_action), training=False)
        if self.max_q_backup is True:
            target_q_values = tf.reduce_mean(tf.reshape(target_q_values, shape=(self.n, batch_size)), axis=0)
            if self.kl_backup is True:
                kl_loss = self._compute_kl_behavior_v2(next_obs, next_raw_action, pi_distribution)  # (None, act_dim)
                kl_loss = tf.reduce_mean(tf.reshape(kl_loss, shape=(self.n, batch_size)), axis=0)
                target_q_values = target_q_values - alpha * (kl_loss - self.delta_behavior)
        else:
            if self.kl_backup is True:
                kl_loss = self._compute_kl_behavior_v2(next_obs, next_raw_action, pi_distribution)  # (None, act_dim)
                target_q_values = target_q_values - alpha * tf.minimum(kl_loss, self.max_kl_backup)

        q_target = reward + self.gamma * (1.0 - done) * target_q_values
        return q_target

    def _compute_q_net_gp(self, obs):
        batch_size = tf.shape(obs)[0]
        if self.reg_type in ['kl', 'cross_entropy']:
            action, log_prob, raw_action, pi_distribution = self.policy_net((obs, False))
            kl = self._compute_kl_behavior_v2(obs, raw_action, pi_distribution)  # (None,)
        elif self.reg_type == 'mmd':
            obs = tf.tile(obs, (self.n, 1))
            action, log_prob, raw_action, pi_distribution = self.policy_net((obs, False))
            kl = self._compute_mmd(obs, raw_action, pi_distribution)
        else:
            raise NotImplementedError

        with tf.GradientTape() as inner_tape:
            inner_tape.watch(action)
            q_values = self.q_network((obs, action), training=False)  # (num_ensembles, None)
        input_gradient = inner_tape.gradient(q_values, action)  # (None, act_dim)
        penalty = tf.norm(input_gradient, axis=-1)  # (None,)
        if self.reg_type == 'mmd':
            penalty = tf.reshape(penalty, shape=(self.n, batch_size))
            penalty = tf.reduce_mean(penalty, axis=0)
        # TODO: consider using soft constraints instead of hard clip
        weights = tf.nn.softplus((kl - self.delta_behavior) * self.sensitivity)
        weights = weights / tf.reduce_max(weights)
        penalty = penalty * tf.stop_gradient(weights)
        return penalty

    def _update_q_nets(self, obs, actions, q_target):
        # q loss
        with tf.GradientTape() as q_tape:
            q_tape.watch(self.q_network.trainable_variables)
            q_values = self.q_network((obs, actions), training=True)  # (num_ensembles, None)
            q_values_loss = 0.5 * tf.square(tf.expand_dims(q_target, axis=0) - q_values)
            # (num_ensembles, None)
            q_values_loss = tf.reduce_sum(q_values_loss, axis=0)  # (None,)

            if self.use_gp:
                gp_weight = self.log_gp(obs)
                gp = self._compute_q_net_gp(obs)
                loss = q_values_loss + gp * gp_weight
            else:
                loss = q_values_loss
            loss = tf.reduce_mean(loss, axis=0)

        minimize(loss, q_tape, self.q_network)

        if self.use_gp:
            with tf.GradientTape() as gp_weight_tape:
                gp_weight_tape.watch(self.log_gp.trainable_variables)
                gp_weight = self.log_gp(obs)
                delta_gp = (gp - self.max_ood_grad_norm) * gp_weight
                loss_gp_weight = -tf.reduce_mean(delta_gp, axis=0)

            minimize(loss_gp_weight, gp_weight_tape, self.log_gp)
        else:
            gp = 0.
            gp_weight = 0.

        info = dict(
            Q1Vals=q_values[0],
            Q2Vals=q_values[1],
            LossQ=q_values_loss,
            GP=gp,
            GPWeight=gp_weight,
        )
        return info

    @tf.function
    def update_q_nets(self, obs, actions, next_obs, done, reward):
        """Normal SAC update"""
        q_target = self._compute_target_q(next_obs, reward, done)
        return self._update_q_nets(obs, actions, q_target)

    @tf.function
    def _update(self, obs, act, obs2, done, rew):
        raw_act = self.behavior_policy.inverse_transform_action(act)
        behavior_loss = self.behavior_policy.train_on_batch(x=(raw_act, obs))['loss']
        info = self.update_q_nets(obs, act, obs2, done, rew)
        actor_info = self.update_actor_first_order(obs)
        self.update_target()
        # we only update alpha when policy is updated
        info.update(actor_info)
        info['BehaviorLoss'] = behavior_loss
        return info

    def update(self, replay_buffer: PyUniformParallelEnvReplayBuffer):
        # TODO: use different batches to update q and actor to break correlation
        data = replay_buffer.sample()
        info = self._update(**data)
        self.logger.store(**to_numpy_or_python_type(info))

    @tf.function
    def act_batch(self, obs, deterministic=True):
        print(f'Tracing act_batch with obs {obs}')
        if deterministic:
            pi_final, log_prob, raw_action, pi_distribution = self.policy_net((obs, deterministic))
        else:
            n = 20
            batch_size = tf.shape(obs)[0]
            obs = tf.tile(obs, (n, 1))
            action = self.policy_net((obs, False))[0]
            q_values_pi_min = self.q_network((obs, action), training=True)[0, :]
            action = tf.reshape(action, shape=(n, batch_size, self.ac_dim))
            idx = tf.argmax(tf.reshape(q_values_pi_min, shape=(n, batch_size)), axis=0,
                            output_type=tf.int32)  # (batch_size)
            idx = tf.stack([idx, tf.range(batch_size)], axis=-1)
            pi_final = tf.gather_nd(action, idx)
        return pi_final


class BRACPRunner(TFRunner):
    def get_action_batch(self, o, deterministic=False):
        return self.agent.act_batch(tf.convert_to_tensor(o, dtype=tf.float32),
                                    deterministic).numpy()

    def test_agent(self, agent, name, deterministic=False, logger=None):
        o, d, ep_ret, ep_len = self.env.reset(), np.zeros(shape=self.num_test_episodes, dtype=np.bool), \
                               np.zeros(shape=self.num_test_episodes), np.zeros(shape=self.num_test_episodes,
                                                                                dtype=np.int64)
        t = tqdm(total=1, desc=f'Testing {name}')
        while not np.all(d):
            a = agent.act_batch(tf.convert_to_tensor(o, dtype=tf.float32),
                                tf.convert_to_tensor(deterministic)).numpy()
            assert not np.any(np.isnan(a)), f'nan action: {a}'
            o, r, d_, _ = self.env.step(a)
            ep_ret = r * (1 - d) + ep_ret
            ep_len = np.ones(shape=self.num_test_episodes, dtype=np.int64) * (1 - d) + ep_len
            d = np.logical_or(d, d_)
        t.update(1)
        t.close()
        normalized_ep_ret = self.dummy_env.get_normalized_score(ep_ret) * 100

        if logger is not None:
            logger.store(TestEpRet=ep_ret, NormalizedTestEpRet=normalized_ep_ret, TestEpLen=ep_len)
        else:
            print(f'EpRet: {np.mean(ep_ret):.2f}, TestEpLen: {np.mean(ep_len):.2f}')

    def setup_replay_buffer(self,
                            batch_size,
                            reward_scale=True):
        import d4rl
        def rescale(x):
            return (x - np.min(x)) / (np.max(x) - np.min(x))

        self.dummy_env = gym.make(self.env_name)
        dataset = d4rl.qlearning_dataset(env=self.dummy_env)

        if reward_scale:
            EpochLogger.log('Using reward scale', color='red')
            dataset['rewards'] = rescale(dataset['rewards'])
        # modify keys
        dataset['obs'] = dataset.pop('observations')
        dataset['act'] = dataset.pop('actions')
        dataset['obs2'] = dataset.pop('next_observations')
        dataset['rew'] = dataset.pop('rewards')
        dataset['done'] = dataset.pop('terminals').astype(np.float32)
        replay_size = dataset['obs'].shape[0]
        self.logger.log(f'Dataset size: {replay_size}')
        self.replay_buffer = PyUniformParallelEnvReplayBuffer.from_data_dict(
            data=dataset,
            batch_size=batch_size
        )

    def setup_agent(self,
                    num_ensembles,
                    behavior_mlp_hidden,
                    behavior_lr,
                    policy_mlp_hidden,
                    q_mlp_hidden,
                    policy_lr,
                    q_lr,
                    alpha_lr,
                    alpha,
                    tau,
                    gamma,
                    target_entropy,
                    use_gp,
                    policy_behavior_lr,
                    reg_type,
                    sigma,
                    n,
                    gp_weight,
                    entropy_reg,
                    kl_backup
                    ):
        obs_dim = self.env.single_observation_space.shape[-1]
        act_dim = self.env.single_action_space.shape[-1]
        self.policy_lr = policy_lr
        self.policy_behavior_lr = policy_behavior_lr
        self.agent = BRACPAgent(ob_dim=obs_dim, ac_dim=act_dim,
                                num_ensembles=num_ensembles,
                                behavior_mlp_hidden=behavior_mlp_hidden,
                                behavior_lr=behavior_lr,
                                policy_mlp_hidden=policy_mlp_hidden, q_mlp_hidden=q_mlp_hidden,
                                q_lr=q_lr, alpha_lr=alpha_lr, alpha=alpha, tau=tau, gamma=gamma,
                                target_entropy=target_entropy, use_gp=use_gp,
                                reg_type=reg_type, sigma=sigma, n=n, gp_weight=gp_weight,
                                entropy_reg=entropy_reg, kl_backup=kl_backup)
        self.agent.set_logger(self.logger)
        self.behavior_filepath = os.path.join(self.logger.output_dir, 'behavior.ckpt')
        self.policy_behavior_filepath = os.path.join(self.logger.output_dir,
                                                     f'policy_behavior_{target_entropy}_{reg_type}.ckpt')
        self.log_beta_behavior_filepath = os.path.join(self.logger.output_dir,
                                                       f'policy_behavior_log_beta_{target_entropy}_{reg_type}.ckpt')
        self.final_filepath = os.path.join(self.logger.output_dir, 'agent_final.ckpt')

    def setup_extra(self,
                    pretrain_epochs,
                    save_freq,
                    max_kl,
                    force_pretrain_behavior,
                    force_pretrain_cloning,
                    generalization_threshold,
                    std_scale
                    ):
        self.pretrain_epochs = pretrain_epochs
        self.save_freq = save_freq
        self.max_kl = max_kl
        self.force_pretrain_behavior = force_pretrain_behavior
        self.force_pretrain_cloning = force_pretrain_cloning
        self.generalization_threshold = generalization_threshold
        self.std_scale = std_scale

    def run_one_step(self, t):
        self.agent.update(self.replay_buffer)

    def on_epoch_end(self, epoch):
        self.test_agent(agent=self.agent, name='policy', logger=self.logger)

        # Log info about epoch
        self.logger.log_tabular('Epoch', epoch)
        self.logger.log_tabular('TestEpRet', with_min_and_max=True)
        self.logger.log_tabular('NormalizedTestEpRet', average_only=True)
        self.logger.log_tabular('TestEpLen', average_only=True)
        self.agent.log_tabular()
        self.logger.log_tabular('GradientSteps', epoch * self.steps_per_epoch)
        self.logger.log_tabular('Time', time.time() - self.start_time)
        self.logger.dump_tabular()

        if self.save_freq is not None and (epoch + 1) % self.save_freq == 0:
            self.agent.save_weights(filepath=os.path.join(self.logger.output_dir, f'agent_final_{epoch + 1}.ckpt'))

    def on_train_begin(self):
        self.agent.policy_net.optimizer = get_adam_optimizer(lr=self.policy_behavior_lr)
        interval = self.pretrain_epochs * self.steps_per_epoch // 5
        behavior_lr = self.agent.behavior_lr
        lr_schedule = tf.keras.optimizers.schedules.PiecewiseConstantDecay(
            boundaries=[interval, interval * 2, interval * 3, interval * 4],
            values=[behavior_lr, 0.5 * behavior_lr, 0.1 * behavior_lr, 0.05 * behavior_lr, 0.01 * behavior_lr])
        self.agent.behavior_policy.optimizer = get_adam_optimizer(lr=lr_schedule)
        try:
            if self.force_pretrain_behavior:
                raise ValueError()
            self.agent.behavior_policy.load_weights(filepath=self.behavior_filepath).assert_consumed()
            EpochLogger.log(f'Successfully load behavior policy from {self.behavior_filepath}')
        except:
            self.pretrain_behavior_policy(self.pretrain_epochs)
            self.agent.behavior_policy.save_weights(filepath=self.behavior_filepath)

        obs_act_dataset = tf.data.Dataset.from_tensor_slices((self.replay_buffer.get()['obs'],
                                                              self.replay_buffer.get()['act'])).batch(1000)
        # evaluate dataset log probability
        behavior_nll = []
        for obs, act in obs_act_dataset:
            raw_act = self.agent.behavior_policy.inverse_transform_action(act)
            behavior_nll.append(self.agent.behavior_policy.test_on_batch(x=(raw_act, obs))['loss'])
        behavior_nll = tf.reduce_mean(tf.concat(behavior_nll, axis=0)).numpy()
        self.logger.log(f'Behavior policy data log probability is {-behavior_nll:.4f}')
        # set target_entropy heuristically as -behavior_log_prob - act_dim
        if self.agent.target_entropy is None:
            # std reduced by a factor of x
            self.agent.target_entropy = behavior_nll - self.agent.ac_dim * np.log(self.std_scale)

        self.logger.log(f'The target entropy of the behavior policy is {self.agent.target_entropy:.4f}')

        try:
            if self.force_pretrain_cloning:
                raise ValueError()
            self.agent.policy_net.load_weights(filepath=self.policy_behavior_filepath).assert_consumed()
            self.agent.log_beta.load_weights(filepath=self.log_beta_behavior_filepath).assert_consumed()
            EpochLogger.log(f'Successfully load initial policy from {self.policy_behavior_filepath}')
        except:
            self.pretrain_cloning(self.pretrain_epochs)
            self.agent.policy_net.save_weights(filepath=self.policy_behavior_filepath)
            self.agent.log_beta.save_weights(filepath=self.log_beta_behavior_filepath)

        hard_update(self.agent.target_policy_net, self.agent.policy_net)
        # reset policy net learning rate
        self.agent.policy_net.optimizer = get_adam_optimizer(lr=self.policy_lr)
        self.agent.log_beta.optimizer = get_adam_optimizer(lr=1e-3)

        # test behavior policy
        self.test_agent(self.agent.behavior_policy, name='vae policy')
        self.test_agent(self.agent, deterministic=True, name='behavior cloning')
        # compute the current KL between pi and pi_b

        distance = []
        for obs, act in obs_act_dataset:
            distance.append(self.agent.compute_pi_pib_distance(obs)[0])
        distance = tf.reduce_mean(tf.concat(distance, axis=0)).numpy()

        self.logger.log(f'The average distance ({self.agent.reg_type}) between pi and pi_b is {distance:.4f}')
        # set max_kl heuristically if it is None.
        if self.max_kl is None:
            self.max_kl = distance + self.generalization_threshold  # allow space to explore generalization

        self.agent.set_delta_behavior(self.max_kl)

        self.start_time = time.time()

    def on_train_end(self):
        self.agent.save_weights(filepath=self.final_filepath)

    def pretrain_cloning(self, epochs):
        EpochLogger.log(f'Training cloning policy for {self.env_name}')
        t = trange(epochs)
        for epoch in t:
            kl, log_pi = [], []
            for _ in trange(self.steps_per_epoch, desc=f'Epoch {epoch + 1}/{epochs}', leave=False):
                # update q_b, pi_0, pi_b
                data = self.replay_buffer.sample()
                obs = data['obs']
                actor_info = self.agent.update_actor_cloning(obs)
                kl.append(actor_info['KL'])
                log_pi.append(actor_info['LogPi'])
            kl = tf.reduce_mean(kl).numpy()
            log_pi = tf.reduce_mean(log_pi).numpy()
            t.set_description(desc=f'KL: {kl:.2f}, LogPi: {log_pi:.2f}')

    def pretrain_behavior_policy(self, epochs):
        EpochLogger.log(f'Training behavior policy for {self.env_name}')
        t = trange(epochs)
        for epoch in t:
            loss = []
            for _ in trange(self.steps_per_epoch, desc=f'Epoch {epoch + 1}/{epochs}', leave=False):
                # update q_b, pi_0, pi_b
                data = self.replay_buffer.sample()
                obs = data['obs']
                raw_act = self.agent.behavior_policy.inverse_transform_action(data['act'])
                behavior_loss = self.agent.behavior_policy.train_on_batch(x=(raw_act, obs))['loss']
                loss.append(behavior_loss)
            loss = tf.reduce_mean(loss).numpy()
            t.set_description(desc=f'Loss: {loss:.2f}')


def bracp(env_name,
          steps_per_epoch=2500,
          pretrain_epochs=200,
          pretrain_behavior=False,
          pretrain_cloning=False,
          epochs=400,
          batch_size=100,

          num_test_episodes=20,
          seed=1,
          # agent args
          policy_mlp_hidden=256,
          q_mlp_hidden=256,
          policy_lr=5e-6,
          policy_behavior_lr=3e-4,
          q_lr=3e-4,
          alpha_lr=1e-3,
          alpha=10.0,
          tau=1e-3,
          gamma=0.99,
          target_entropy=None,
          max_kl=None,
          use_gp=True,
          reg_type='kl',
          sigma=20,
          n=5,
          gp_weight=0.1,
          entropy_reg=True,
          kl_backup=False,
          generalization_threshold=0.1,
          std_scale=4.,
          # behavior policy
          num_ensembles=1,
          behavior_mlp_hidden=256,
          behavior_lr=1e-3,
          # others
          reward_scale=True,
          save_freq=None,
          ):
    """Main function to run Improved Behavior Regularized Actor Critic (BRAC+)

    Args:
        env_name (str): name of the environment
        steps_per_epoch (int): number of steps per epoch
        pretrain_epochs (int): number of epochs to pretrain
        pretrain_behavior (bool): whether to pretrain the behavior policy or load from checkpoint.
            If load fails, the flag is ignored.
        pretrain_cloning (bool):whether to pretrain the initial policy or load from checkpoint.
            If load fails, the flag is ignored.
        epochs (int): number of epochs to run
        batch_size (int): batch size of the data sampled from the dataset
        num_test_episodes (int): number of test episodes to evaluate the policy after each epoch
        seed (int): random seed
        policy_mlp_hidden (int): MLP hidden size of the policy network
        q_mlp_hidden (int): MLP hidden size of the Q network
        policy_lr (float): learning rate of the policy network
        policy_behavior_lr (float): learning rate used to train the policy that minimize the distance between the policy
            and the behavior policy. This is usally larger than policy_lr.
        q_lr (float): learning rate of the q network
        alpha_lr (float): learning rate of the alpha
        alpha (int): initial Lagrange multiplier used to control the maximum distance between the \pi and \pi_b
        tau (float): polyak average coefficient of the target update
        gamma (float): discount factor
        target_entropy (float or None): target entropy of the policy
        max_kl (float or None): maximum of the distance between \pi and \pi_b
        use_gp (bool): whether use gradient penalty or not
        reg_type (str): regularization type
        sigma (float): sigma of the Laplacian kernel for MMD
        n (int): number of samples when estimate the expectation for policy evaluation and update
        gp_weight (float): initial GP weight
        entropy_reg (bool): whether use entropy regularization or not
        kl_backup (bool): whether add the KL loss to the backup value of the target Q network
        generalization_threshold (float): generalization threshold used to compute max_kl when max_kl is None
        std_scale (float): standard deviation scale when computing target_entropy when it is None.
        num_ensembles (int): number of ensembles to train the behavior policy
        behavior_mlp_hidden (int): MLP hidden size of the behavior policy
        behavior_lr (float): the learning rate of the behavior policy
        reward_scale (float): whether to use reward scale or not. By default, it will scale to [0, 1]
        save_freq (int or None): the frequency to save the model

    Returns: None

    """

    config = locals()

    runner = BRACPRunner(seed=seed, steps_per_epoch=steps_per_epoch, epochs=epochs,
                         exp_name=None, logger_path='data')
    runner.setup_env(env_name=env_name, num_parallel_env=num_test_episodes, frame_stack=None, wrappers=None,
                     asynchronous=False, num_test_episodes=None)
    runner.setup_logger(config=config)
    runner.setup_agent(num_ensembles=num_ensembles,
                       behavior_mlp_hidden=behavior_mlp_hidden,
                       behavior_lr=behavior_lr,
                       policy_mlp_hidden=policy_mlp_hidden, q_mlp_hidden=q_mlp_hidden,
                       policy_lr=policy_lr, q_lr=q_lr, alpha_lr=alpha_lr, alpha=alpha, tau=tau, gamma=gamma,
                       target_entropy=target_entropy, use_gp=use_gp,
                       policy_behavior_lr=policy_behavior_lr,
                       reg_type=reg_type, sigma=sigma, n=n, gp_weight=gp_weight,
                       entropy_reg=entropy_reg, kl_backup=kl_backup)
    runner.setup_extra(pretrain_epochs=pretrain_epochs,
                       save_freq=save_freq,
                       max_kl=max_kl,
                       force_pretrain_behavior=pretrain_behavior,
                       force_pretrain_cloning=pretrain_cloning,
                       generalization_threshold=generalization_threshold,
                       std_scale=std_scale)
    runner.setup_replay_buffer(batch_size=batch_size,
                               reward_scale=reward_scale)

    runner.run()


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument('--env_name', type=str, required=True)
    parser.add_argument('--pretrain_behavior', action='store_true')
    parser.add_argument('--pretrain_cloning', action='store_true')
    parser.add_argument('--seed', type=int, default=1)

    args = vars(parser.parse_args())
    env_name = args['env_name']

    bracp(**args)
