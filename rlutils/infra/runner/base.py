"""
Common in the runner:
1. Setup environment
2. Setup logger
3. Setup agent
4. Run
"""

import random
import time
from abc import abstractmethod, ABC

import numpy as np
import rlutils.gym
import rlutils.infra as rl_infra
from rlutils.logx import EpochLogger, setup_logger_kwargs
from rlutils.replay_buffers import PyUniformParallelEnvReplayBuffer, GAEBuffer
from tqdm.auto import trange


class BaseRunner(ABC):
    def __init__(self, seed, steps_per_epoch, epochs, exp_name=None, logger_path='data'):
        self.exp_name = exp_name
        self.logger_path = logger_path
        self.steps_per_epoch = steps_per_epoch
        self.epochs = epochs
        self.seed = seed
        self.global_step = 0
        self.seeder = rl_infra.Seeder(seed=seed)
        self.timer = rl_infra.StopWatch()
        self.agent = None
        self.setup_global_seed()

    def setup_logger(self, config, tensorboard=False):
        if self.exp_name is None:
            self.exp_name = f'{self.env_name}_{self.agent.__class__.__name__}_test'
        assert self.exp_name is not None, 'Call setup_env before setup_logger if exp passed by the contructor is None.'
        logger_kwargs = setup_logger_kwargs(exp_name=self.exp_name, data_dir=self.logger_path, seed=self.seed)
        self.logger = EpochLogger(**logger_kwargs, tensorboard=tensorboard)
        self.logger.save_config(config)

        self.timer.set_logger(logger=self.logger)
        self.agent.set_logger(logger=self.logger)

    def setup_global_seed(self):
        # we set numpy seed first and use it to generate other seeds
        np.random.seed(self.seeder.generate_seed())
        random.seed(self.seeder.generate_seed())

    @abstractmethod
    def run_one_step(self, t):
        raise NotImplementedError

    def on_epoch_begin(self, epoch):
        pass

    def on_epoch_end(self, epoch):
        pass

    def on_train_begin(self):
        pass

    def on_train_end(self):
        pass

    def setup_env(self,
                  env_name,
                  env_fn=None,
                  num_parallel_env=1,
                  asynchronous=False,
                  num_test_episodes=None):
        self.env_name = env_name
        self.env = rlutils.gym.utils.create_vector_env(env_name=env_name,
                                                       env_fn=env_fn,
                                                       normalize_action_space=True,
                                                       num_parallel_env=num_parallel_env,
                                                       asynchronous=asynchronous)
        self.env.seed(self.seeder.generate_seed())
        self.env.action_space.seed(self.seeder.generate_seed())
        if num_test_episodes is not None:
            self.test_env = rlutils.gym.utils.create_vector_env(env_name=env_name,
                                                                env_fn=env_fn,
                                                                normalize_action_space=True,
                                                                num_parallel_env=num_test_episodes,
                                                                asynchronous=asynchronous)
            self.test_env.seed(self.seeder.generate_seed())
            self.test_env.action_space.seed(self.seeder.generate_seed())

    def setup_agent(self, agent_cls, **kwargs):
        self.agent = agent_cls(obs_spec=self.env.single_observation_space,
                               act_spec=self.env.single_action_space,
                               **kwargs)

    def run(self):
        self.on_train_begin()
        for i in range(1, self.epochs + 1):
            self.on_epoch_begin(i)
            for t in trange(self.steps_per_epoch, desc=f'Epoch {i}/{self.epochs}'):
                self.run_one_step(t)
                self.global_step += 1
            self.on_epoch_end(i)
        self.on_train_end()

    @classmethod
    def main(cls, *args, **kwargs):
        raise NotImplementedError

    def save_checkpoint(self, path=None):
        pass

    def load_checkpoint(self, path=None):
        pass


class OnPolicyRunner(BaseRunner):
    def setup_logger(self, config, tensorboard=False):
        super(OnPolicyRunner, self).setup_logger(config=config, tensorboard=tensorboard)
        self.sampler.set_logger(self.logger)
        self.updater.set_logger(self.logger)

    def setup_replay_buffer(self, max_length, gamma, lam):
        self.replay_buffer = GAEBuffer.from_vec_env(self.env, max_length=max_length, gamma=gamma, lam=lam)

    def setup_sampler(self, num_steps):
        self.num_steps = num_steps
        self.sampler = rl_infra.samplers.TrajectorySampler(env=self.env)

    def setup_updater(self):
        self.updater = rl_infra.OnPolicyUpdater(agent=self.agent, replay_buffer=self.replay_buffer)

    def run_one_step(self, t):
        self.sampler.sample(num_steps=self.num_steps,
                            collect_fn=(self.agent.act_batch, self.agent.value_net.predict),
                            replay_buffer=self.replay_buffer)
        self.updater.update()

    def on_epoch_end(self, epoch):
        self.logger.log_tabular('Epoch', epoch)
        self.sampler.log_tabular()
        self.updater.log_tabular()
        self.timer.log_tabular()
        self.logger.dump_tabular()

    def on_train_begin(self):
        self.sampler.reset()
        self.updater.reset()
        self.timer.start()

    @classmethod
    def main(cls, env_name, env_fn=None, seed=0, num_parallel_envs=5, agent_cls=None, agent_kwargs={},
             batch_size=5000, epochs=200, gamma=0.99, lam=0.97, logger_path: str = None):
        # Instantiate environment
        assert batch_size % num_parallel_envs == 0

        num_steps_per_sample = batch_size // num_parallel_envs

        config = locals()
        runner = cls(seed=seed, steps_per_epoch=1,
                     epochs=epochs, exp_name=None, logger_path=logger_path)
        runner.setup_env(env_name=env_name, env_fn=env_fn, num_parallel_env=num_parallel_envs,
                         asynchronous=False, num_test_episodes=None)
        runner.setup_agent(agent_cls=agent_cls, **agent_kwargs)
        runner.setup_replay_buffer(max_length=num_steps_per_sample, gamma=gamma, lam=lam)
        runner.setup_sampler(num_steps=num_steps_per_sample)
        runner.setup_updater()
        runner.setup_logger(config)

        runner.run()


class OffPolicyRunner(BaseRunner):
    def setup_logger(self, config, tensorboard=False):
        super(OffPolicyRunner, self).setup_logger(config=config, tensorboard=tensorboard)
        self.sampler.set_logger(self.logger)
        self.tester.set_logger(self.logger)
        self.updater.set_logger(self.logger)

    def setup_tester(self, num_test_episodes):
        self.num_test_episodes = num_test_episodes
        self.tester = rl_infra.Tester(self.test_env)

    def setup_replay_buffer(self,
                            replay_size,
                            batch_size):
        self.replay_buffer = PyUniformParallelEnvReplayBuffer.from_vec_env(self.env, capacity=replay_size,
                                                                           batch_size=batch_size)

    def setup_sampler(self, start_steps, num_steps):
        self.start_steps = start_steps
        self.num_steps = num_steps
        self.sampler = rl_infra.samplers.BatchSampler(env=self.env)

    def setup_updater(self, update_after, policy_delay, update_per_step):
        self.update_after = update_after
        self.updater = rl_infra.OffPolicyUpdater(agent=self.agent,
                                                 replay_buffer=self.replay_buffer,
                                                 policy_delay=policy_delay,
                                                 update_per_step=update_per_step)

    def run_one_step(self, t):
        if self.sampler.total_env_steps < self.start_steps:
            self.sampler.sample(num_steps=1,
                                collect_fn=lambda o: np.asarray(self.env.action_space.sample()),
                                replay_buffer=self.replay_buffer)
        else:
            self.sampler.sample(num_steps=self.num_steps,
                                collect_fn=lambda obs: self.agent.act_batch_explore(obs),
                                replay_buffer=self.replay_buffer)
        # Update handling
        if self.sampler.total_env_steps >= self.update_after:
            self.updater.update()

    def on_epoch_end(self, epoch):
        self.tester.test_agent(get_action=lambda obs: self.agent.act_batch_test(obs),
                               name=self.agent.__class__.__name__,
                               num_test_episodes=self.num_test_episodes)
        # Log info about epoch
        self.logger.log_tabular('Epoch', epoch)
        self.tester.log_tabular()
        self.sampler.log_tabular()
        self.updater.log_tabular()
        self.timer.log_tabular()
        self.logger.dump_tabular()

    def on_train_begin(self):
        self.sampler.reset()
        self.updater.reset()
        self.timer.start()

    @classmethod
    def main(cls,
             env_name,
             env_fn=None,
             steps_per_epoch=5000,
             epochs=200,
             start_steps=10000,
             update_after=4000,
             update_every=1,
             update_per_step=1,
             policy_delay=1,
             batch_size=256,
             num_parallel_env=1,
             num_test_episodes=30,
             seed=1,
             # agent args
             agent_cls=None,
             agent_kwargs={},
             # replay
             replay_size=int(1e6),
             logger_path=None
             ):
        config = locals()

        runner = cls(seed=seed, steps_per_epoch=steps_per_epoch, epochs=epochs,
                     exp_name=None, logger_path=logger_path)
        runner.setup_env(env_name=env_name, env_fn=env_fn, num_parallel_env=num_parallel_env,
                         asynchronous=False, num_test_episodes=num_test_episodes)
        runner.setup_agent(agent_cls=agent_cls, **agent_kwargs)
        runner.setup_replay_buffer(replay_size=replay_size,
                                   batch_size=batch_size)
        runner.setup_sampler(start_steps=start_steps, num_steps=update_every)
        runner.setup_tester(num_test_episodes=num_test_episodes)
        runner.setup_updater(update_after=update_after,
                             policy_delay=policy_delay,
                             update_per_step=update_every * update_per_step)
        runner.setup_logger(config=config, tensorboard=False)
        runner.run()


class OfflineRunner(BaseRunner):
    def setup_replay_buffer(self,
                            batch_size,
                            dataset=None,
                            reward_scale=True):
        def rescale(x):
            return (x - np.min(x)) / (np.max(x) - np.min(x))

        if dataset is None:
            # modify d4rl keys
            import d4rl
            dataset = d4rl.qlearning_dataset(env=self.dummy_env)
            dataset['obs'] = dataset.pop('observations').astype(np.float32)
            dataset['act'] = dataset.pop('actions').astype(np.float32)
            dataset['next_obs'] = dataset.pop('next_observations').astype(np.float32)
            dataset['rew'] = dataset.pop('rewards').astype(np.float32)
            dataset['done'] = dataset.pop('terminals').astype(np.float32)

        if reward_scale:
            EpochLogger.log('Using reward scale', color='red')
            self.agent.reward_scale_factor = np.max(dataset['rew'] - np.min(dataset['rew']))
            EpochLogger.log(f'The scale factor is {self.agent.reward_scale_factor:.2f}')
            dataset['rew'] = rescale(dataset['rew'])

        replay_size = dataset['obs'].shape[0]
        EpochLogger.log(f'Dataset size: {replay_size}')
        self.replay_buffer = PyUniformParallelEnvReplayBuffer.from_data_dict(
            data=dataset,
            batch_size=batch_size
        )

    def setup_agent(self, agent_cls, **kwargs):
        self.agent = agent_cls(obs_spec=self.obs_data_spec, act_spec=self.act_data_spec, **kwargs)
        self.agent.set_logger(self.logger)

    def on_epoch_end(self, epoch):
        info = self.test_agent(get_action=self.get_action_batch_test, name=self.agent.__class__.__name__)
        self.logger.store(**info)

        # Log info about epoch
        self.logger.log_tabular('Epoch', epoch)
        self.logger.log_tabular('TestEpRet', with_min_and_max=True)
        self.logger.log_tabular('TestEpLen', average_only=True)
        self.agent.log_tabular()
        self.logger.log_tabular('GradientSteps', epoch * self.steps_per_epoch)
        self.logger.log_tabular('Time', time.time() - self.start_time)
        self.logger.dump_tabular()
