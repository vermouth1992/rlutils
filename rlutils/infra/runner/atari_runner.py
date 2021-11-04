import gym
from gym.wrappers import AtariPreprocessing

import rlutils.infra as rl_infra
from rlutils.replay_buffers import PyMemoryEfficientReplayBuffer
from .base import OffPolicyRunner


class AtariRunner(OffPolicyRunner):
    def setup_replay_buffer(self,
                            replay_size,
                            batch_size):
        self.seeds_info['replay_buffer'] = self.seeder.generate_seed()
        self.replay_buffer = PyMemoryEfficientReplayBuffer.from_vec_env(self.env, capacity=replay_size,
                                                                        batch_size=batch_size,
                                                                        seed=self.seeds_info['replay_buffer'])

    def setup_env(self,
                  env_name,
                  env_fn=None,
                  num_parallel_env=1,
                  asynchronous=False,
                  num_test_episodes=None):
        assert env_fn is None
        env_fn = lambda: AtariPreprocessing(gym.make(env_name))
        # we handle frame stack in the sampler
        super(AtariRunner, self).setup_env(env_name=env_name, env_fn=env_fn,
                                           num_parallel_env=num_parallel_env,
                                           asynchronous=asynchronous,
                                           num_test_episodes=num_test_episodes)

    def setup_sampler(self, start_steps):
        self.start_steps = start_steps
        self.sampler = rl_infra.samplers.BatchFrameStackSampler(env=self.env)