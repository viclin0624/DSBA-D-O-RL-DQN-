from State import AI_Board
import parl
from parl.core.fluid import layers
import paddle.fluid as fluid
import paddle
import cv2
from parl.algorithms import DQN
import numpy as np
import random
import collections
from datetime import datetime
from resnet import DistResNet
import os
import sys
import pickle

class Model(parl.Model):
    def __init__(self, act_dim):
        self.act_dim = act_dim
        self.net = DistResNet()

    def value(self, obs):
        # 定义网络
        Q = self.net.infer(obs, class_dim=self.act_dim)
        return Q


class Agent(parl.Agent):
    def __init__(self,
                 algorithm,
                 obs_dim,
                 act_dim,
                 e_greed=0.1,
                 e_greed_decrement=0):
        assert isinstance(obs_dim, list)
        assert isinstance(act_dim, int)
        self.obs_dim = obs_dim
        self.act_dim = act_dim
        super(Agent, self).__init__(algorithm)

        self.global_step = 0
        self.update_target_steps = 200  # 每隔200个training steps再把model的参数复制到target_model中

        self.e_greed = e_greed  # 有一定概率随机选取动作，探索
        self.e_greed_decrement = e_greed_decrement  # 随着训练逐步收敛，探索的程度慢慢降低

        self.agent_run_times = 0

    def build_program(self):
        self.pred_program = fluid.Program()
        self.learn_program = fluid.Program()

        with fluid.program_guard(self.pred_program):  # 搭建计算图用于 预测动作，定义输入输出变量
            obs = layers.data(name='obs', shape=self.obs_dim, dtype='float32')
            self.value = self.alg.predict(obs)

        with fluid.program_guard(self.learn_program):  # 搭建计算图用于 更新Q网络，定义输入输出变量
            obs = layers.data(
                name='obs', shape=self.obs_dim, dtype='float32')
            action = layers.data(name='act', shape=[1], dtype='int32')
            reward = layers.data(name='reward', shape=[], dtype='float32')
            next_obs = layers.data(
                name='next_obs', shape=self.obs_dim, dtype='float32')
            terminal = layers.data(name='terminal', shape=[], dtype='bool')
            self.cost = self.alg.learn(obs, action, reward, next_obs, terminal)

    def sample(self, obs):
        sample = np.random.rand()  # 产生0~1之间的小数
        if sample < self.e_greed:
            act = np.random.randint(self.act_dim)
            # act = 0 # 探索：每个动作都有概率被选择
        else:
            act = self.predict(obs)  # 选择最优动作
        #act = 4
        #self.e_greed = max(0.01, self.e_greed - self.e_greed_decrement)  # 随着训练逐步收敛，探索的程度慢慢降低
        self.e_greed = max(0.1, self.e_greed - self.e_greed_decrement)# 随着训练逐步收敛，探索的程度慢慢降低
        return act

    def predict(self, obs):  # 选择最优动作
        if len(obs.shape) == 3:
            obs = np.expand_dims(obs, axis=0)
        pred_Q = self.fluid_executor.run(
            self.pred_program,
            feed={'obs': obs.astype('float32')},
            fetch_list=[self.value])[0]
        pred_Q = np.squeeze(pred_Q, axis=0)
        #print(pred_Q)
        act = np.argmax(pred_Q)  # 选择Q最大的下标，即对应的动作
        return act

    def learn(self, obs, act, reward, next_obs, terminal):
        # 每隔200个training steps同步一次model和target_model的参数
        if self.global_step % self.update_target_steps == 0:
            self.alg.sync_target()
        self.global_step += 1

        act = np.expand_dims(act, -1)
        feed = {
            'obs': obs.astype('float32'),
            'act': act.astype('int32'),
            'reward': reward,
            'next_obs': next_obs.astype('float32'),
            'terminal': terminal
        }
        cost = self.fluid_executor.run(
            self.learn_program, feed=feed, fetch_list=[self.cost])[0]  # 训练一次网络
        return cost

    # 保存模型
    def save_params(self, learnDir, predictDir):
        fluid.io.save_params(
            executor=self.fluid_executor,
            dirname=learnDir,
            main_program=self.learn_program)
        fluid.io.save_params(
            executor=self.fluid_executor,
            dirname=predictDir,
            main_program=self.pred_program)

        # 加载模型

    def load_params(self, learnDir, predictDir):
        fluid.io.load_params(
            executor=self.fluid_executor,
            dirname=learnDir,
            main_program=self.learn_program)
        fluid.io.load_params(
            executor=self.fluid_executor,
            dirname=predictDir,
            main_program=self.pred_program)

class ReplayMemory(object):
    def __init__(self, max_size):
        self.buffer = collections.deque(maxlen=max_size)

    # 增加一条经验到经验池中
    def append(self, exp):
        self.buffer.append(exp)

    # 从经验池中选取N条经验出来
    def sample(self, batch_size):
        mini_batch = random.sample(self.buffer, batch_size)
        obs_batch, action_batch, reward_batch, next_obs_batch, done_batch = [], [], [], [], []

        for experience in mini_batch:
            s, a, r, s_p, done = experience
            obs_batch.append(s)
            action_batch.append(a)
            reward_batch.append(r)
            next_obs_batch.append(s_p)
            done_batch.append(done)

        return np.array(obs_batch).astype('float32'), \
            np.array(action_batch).astype('float32'), np.array(reward_batch).astype('float32'),\
            np.array(next_obs_batch).astype('float32'), np.array(
                done_batch).astype('float32')

    def __len__(self):
        return len(self.buffer)


def run_episode(game, agent, rpm):
    # print("1")
    actionset = range(game.action_num)
    total_reward = 0
    image, score, reward, alive = game.next()#初始不扔水果
    obs = process(image)
    #done = not alive
    step = 0
    while True:
        step += 1
        action = agent.sample(obs)  # 采样动作，所有动作都有概率被尝试到
        #print(action," ", end="")
        image, score, reward, alive = game.next(actionset[action])
        next_obs = process(image)
        done = not alive
        if len(obs.shape) == 4:
            obs = obs[0]
        if len(next_obs.shape) == 4:
            next_obs = next_obs[0]
        rpm.append((obs, action, reward, next_obs, done))
        agent.agent_run_times += 1
        print('已经累计经验：'+str(agent.agent_run_times),'目前经验池大小：',str(len(rpm.buffer)))
        # train model
        if (len(rpm) > MEMORY_WARMUP_SIZE) and (step % LEARN_FREQ == 0):
            (batch_obs, batch_action, batch_reward, batch_next_obs,
             batch_done) = rpm.sample(BATCH_SIZE)
            train_loss = agent.learn(batch_obs, batch_action, batch_reward,
                                     batch_next_obs,
                                     batch_done)  # s,a,r,s',done

        total_reward += reward
        obs = next_obs
        if done:
            break
    # print()
    return total_reward

# 评估 agent, 跑 5 个episode，总reward求平均


def evaluate(agent):
    actionset = range(game.action_num)
    eval_reward = []
    for i in range(5):
        image, score, reward, alive = game.next()  # 初始不扔水果
        obs = process(image)
        episode_reward = 0
        while True:
            action = agent.predict(obs)
            #score = game.score()
            image, score, reward, alive = game.next(actionset[action])  # 初始不扔水果
            obs = process(image)

            done = not alive
            episode_reward += reward
            if done:
                break
        eval_reward.append(episode_reward)
        # cv2.destroyAllWindows()
    return np.mean(eval_reward)


def process(input):
    image = cv2.resize(input, (160, 80))
    image = image.swapaxes(1,2).swapaxes(0,1)
    image = np.expand_dims(image, axis=0)
    # scale down pixels values to (0,1)
    image = image / 255.0
    return image

if __name__ == '__main__':
    os.environ["CUDA_VISIBLE_DEVICES"] = "0"
    #paddle.enable_static()
    
    game = AI_Board()

    LEARN_FREQ = 5  # 训练频率，不需要每一个step都learn，攒一些新增经验后再learn，提高效率
    MEMORY_SIZE = 20000    # replay memory的大小，越大越占用内存
    MEMORY_WARMUP_SIZE = 200  # replay_memory 里需要预存一些经验数据，再开启训练 200
    BATCH_SIZE = 32   # 每次给agent learn的数据数量，从replay memory随机里sample一批数据出来32
    LEARNING_RATE = 0.001  # 学习率
    GAMMA = 0.99

    action_dim = game.action_num  # number of valid actions

    obs_shape = [3, 80, 160]

    rpm = ReplayMemory(MEMORY_SIZE)  # DQN的经验回放池

    # 根据parl框架构建agent
    model = Model(act_dim=action_dim)
    algorithm = DQN(model, act_dim=action_dim, gamma=GAMMA, lr=LEARNING_RATE)#
    agent = Agent(
        algorithm,
        obs_dim=obs_shape,
        act_dim=action_dim,
        e_greed=1,  # 有一定概率随机选取动作，探索 改为1
        e_greed_decrement=1e-5)  # 随着训练逐步收敛，探索的程度慢慢降低 大约一天后降为0.1

    # 加载模型
    read_trained_model_dir = './model/'
    if os.path.exists(read_trained_model_dir):
        model_name_list = sorted(os.listdir(read_trained_model_dir))
        if len(model_name_list) > 0:
            latest_episode = np.argmax([int(x.split('_')[1]) for x in model_name_list])
            save_path = os.path.join(read_trained_model_dir, model_name_list[latest_episode])
            print(f'Restore model from {save_path}')
            agent.restore(save_path)

    read_learn_trained_model_dir = './learn/'
    if os.path.exists(read_learn_trained_model_dir):
        model_name_list = sorted(os.listdir(read_learn_trained_model_dir))
        if len(model_name_list) > 0:
            latest_episode = np.argmax([int(x.split('_')[1]) for x in model_name_list])
            learn_save_path = os.path.join(read_learn_trained_model_dir, model_name_list[latest_episode])
            print(f'Restore learn model from {learn_save_path}')
            predict_save_path = os.path.join('./predict/', model_name_list[latest_episode])
            print(f'Restore learn model from {predict_save_path}')
            agent.load_params(learn_save_path,predict_save_path)
    #加载预热的pkl
    if os.path.exists('./rpm.pkl'):
        with open('rpm.pkl','rb') as f_rpm:
            rpm_ex = pickle.load(f_rpm)
        if rpm_ex is not None:
            for i in range(max(len(rpm_ex.buffer)-MEMORY_SIZE,0), len(rpm_ex.buffer)):
                rpm.append(rpm_ex.buffer[i])
            del rpm_ex

    # 先往经验池里存一些数据，避免最开始训练的时候样本丰富度不够
    while len(rpm) < MEMORY_WARMUP_SIZE:
        run_episode(game, agent, rpm)
        if len(rpm) >= MEMORY_WARMUP_SIZE:
            with open('rpm.pkl', 'wb') as f_rpm:
                pickle.dump(rpm, f_rpm)
    print('开始训练')


    max_episode = 200000

    # 开始训练
    episode = 0

    ps = datetime.now()

    evmax = 0
    with open('score.csv','a',encoding='utf-8') as f:
        f.write('episode,score\n')
    with open('test_score.csv','a',encoding='utf-8') as ft:
        ft.write('episode,time,e_greed,test_score\n')
    while episode < max_episode:  # 训练max_episode个回合，test部分不计算入episode数量
        # train part
        start = datetime.now()
        for i in range(0, 25):
            total_reward = run_episode(game, agent, rpm)
            episode += 1
            print('###############################episode:',episode,'score:',total_reward,'###############################')
            with open('score.csv', 'a', encoding='utf-8') as f:
                f.write(str(episode)+','+str(total_reward)+'\n')
        end = datetime.now()

        # test part
        eval_reward = evaluate(agent)  # render=True 查看显示效果
        print('episode:{}    time:{}    e_greed:{}   test_reward:{}'.format(
            episode, (end-start).seconds, agent.e_greed, eval_reward))
        with open('test_score.csv', 'a', encoding='utf-8') as ft:
            ft.write(str(episode)+','+str((end-start).seconds)+','+str(agent.e_greed)+','+str(eval_reward)+'\n')

        # 训练结束，保存模型
        if not os.path.exists('./model/'):
            os.makedirs('./model/')
        save_path = './model/model_' + \
                    str(episode) + '_' + str(eval_reward) + '.ckpt'

        agent.save(save_path)
        agent.save_params('./learn/model_' + \
                    str(episode) + '_' + str(eval_reward) + '.ckpt','./predict/model_' + \
                    str(episode) + '_' + str(eval_reward) + '.ckpt')
        evmax = eval_reward
        with open('rpm'+str(episode)+'.pkl', 'wb') as f_rpm:
            pickle.dump(rpm,f_rpm)