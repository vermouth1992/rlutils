"""
Benchmark SAC and TD3 on update_every=1, 50
TD3 output activation=tf.
"""

import unittest

import rlutils.infra as rl_infra


def thunk(**kwargs):
    from rlutils.algos.tf.mf import sac, td3
    temp = [sac, td3]
    algo = kwargs.pop('algo')
    eval(f'{algo}.Runner.main')(**kwargs)


def thunk_pytorch(**kwargs):
    from rlutils.algos.pytorch.mf import sac as sac_pytorch, td3 as td3_pytorch
    temp = [sac_pytorch, td3_pytorch]
    algo = kwargs.pop('algo')
    eval(f'{algo}.Runner.main')(**kwargs)


class BenchmarkMujoco(unittest.TestCase):
    env_lst = ['Hopper-v2', 'Walker2d-v2', 'HalfCheetah-v2', 'Ant-v2']
    update_lst = [50, 1]
    seeds = list(range(110, 120))

    def test_sac_pytorch_hopper(self):
        algo = 'sac_pytorch'
        experiments = rl_infra.runner.ExperimentGrid()
        experiments.add(key='env_name', vals=self.env_lst[0], shorthand='ENV', in_name=True)
        experiments.add(key='update_every', vals=self.update_lst[0], in_name=True, shorthand='UPDATE')
        experiments.add(key='algo', vals=algo, in_name=True, shorthand='ALG')
        experiments.add(key='epochs', vals=300)
        experiments.add(key='seed', vals=self.seeds)
        experiments.run(thunk=thunk_pytorch, data_dir='benchmark_results')

    def test_sac_update_every(self):
        algo = 'sac'
        experiments = rl_infra.runner.ExperimentGrid()
        experiments.add(key='env_name', vals=self.env_lst, shorthand='ENV', in_name=True)
        experiments.add(key='update_every', vals=self.update_lst, in_name=True, shorthand='UPDATE')
        experiments.add(key='algo', vals=algo, in_name=True, shorthand='ALG')
        experiments.add(key='epochs', vals=300)
        experiments.add(key='seed', vals=self.seeds)
        experiments.run(thunk=thunk, data_dir='benchmark_results')

    def test_td3_update_every(self):
        algo = 'td3'
        experiments = rl_infra.runner.ExperimentGrid()
        experiments.add(key='env_name', vals=self.env_lst, shorthand='ENV')
        experiments.add(key='update_every', vals=self.update_lst, in_name=True, shorthand='UPDATE')
        experiments.add(key='algo', vals=algo, in_name=True, shorthand='ALG')
        experiments.add(key='epochs', vals=300)
        experiments.add(key='seed', vals=self.seeds)
        experiments.run(thunk=thunk, data_dir='benchmark_results')

    def test_td3_out_activation(self):
        algo = 'td3'
        experiments = rl_infra.runner.ExperimentGrid()
        experiments.add(key='env_name', vals=self.env_lst, shorthand='ENV')
        experiments.add(key='update_every', vals=[1], in_name=True, shorthand='UPDATE')
        experiments.add(key='algo', vals=algo, in_name=True, shorthand='ALG')
        experiments.add(key='epochs', vals=300)
        experiments.add(key='seed', vals=self.seeds)
        experiments.add(key='out_activation', vals='sin', in_name=True)
        experiments.run(thunk=thunk, data_dir='benchmark_results')


class BenchmarkMujocoNT(unittest.TestCase):
    env_lst = ['HopperNT-v2', 'Walker2dNT-v2', 'AntNT-v2']
    seeds = list(range(110, 120))

    def test_sac(self):
        algo = 'sac'
        experiments = rl_infra.runner.ExperimentGrid()
        experiments.add(key='env_name', vals=self.env_lst, shorthand='ENV', in_name=True)
        experiments.add(key='algo', vals=algo, in_name=True, shorthand='ALG')
        experiments.add(key='epochs', vals=300)
        experiments.add(key='seed', vals=self.seeds)
        experiments.run(thunk=thunk, data_dir='benchmark_results')

    def test_td3(self):
        algo = 'td3'
        experiments = rl_infra.runner.ExperimentGrid()
        experiments.add(key='env_name', vals=self.env_lst, shorthand='ENV')
        experiments.add(key='algo', vals=algo, in_name=True, shorthand='ALG')
        experiments.add(key='epochs', vals=300)
        experiments.add(key='seed', vals=self.seeds)
        experiments.run(thunk=thunk, data_dir='benchmark_results')


if __name__ == '__main__':
    unittest.main()
