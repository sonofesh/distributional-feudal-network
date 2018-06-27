from itertools import count

import torch
from torch.distributions import Categorical
import torch.optim as optim
from torch.autograd import Variable

from plot import Plotter


def ensure_shared_grads(model, shared_model):
    for param, shared_param in zip(model.parameters(),
                                   shared_model.parameters()):
        if shared_param.grad is not None:
            return
        shared_param._grad = param.grad


def train(
        env,
        model,
        lr,
        alpha,  # intrinsic reward multiplier
        entropy_coef,  # beta
        tau_worker,
        gamma_worker,
        gamma_manager,
        num_steps,
        max_episode_length,
        max_grad_norm,
        value_worker_loss_coef=0.5,
        value_manager_loss_coef=0.5):
    optimizer = optim.Adam(model.parameters(), lr=lr)
    model.train()

    obs = env.reset()
    obs = torch.from_numpy(obs)
    done = True

    plt_loss = Plotter("Loss", ylim_max=1000)
    plt_reward = Plotter("Reward")

    episode_length = 0
    for epoch in count():
        # Sync with the shared model
        #model.load_state_dict(shared_model.state_dict())

        if done:
            states = model.init_state(1)
        else:
            states = model.reset_states_grad(states)

        values_worker, values_manager = [], []
        log_probs = []
        rewards, intrinsic_rewards = [], []
        entropies = []  # regularisation
        manager_partial_loss = []

        for step in range(num_steps):
            episode_length += 1
            value_worker, value_manager, action_probs, goal, nabla_dcos, states = model(obs.unsqueeze(0), states)
            m = Categorical(probs=action_probs)
            action = m.sample()
            log_prob = m.log_prob(action)
            entropy = -(log_prob * action_probs).sum(1, keepdim=True)
            entropies.append(entropy)
            manager_partial_loss.append(nabla_dcos)

            obs, reward, done, _ = env.step(action.numpy())
            done = done or episode_length >= max_episode_length
            reward = max(min(reward, 1), -1)
            intrinsic_reward = model._intrinsic_reward(states)
            intrinsic_reward = float(intrinsic_reward)  # TODO batch

            #plt_reward.add_value(None, intrinsic_reward, "Intrinsic reward")
            #plt_reward.add_value(None, reward, "Reward")
            #plt_reward.draw()

            #with lock:
            #    counter.value += 1

            if done:
                episode_length = 0
                obs = env.reset()

            obs = torch.from_numpy(obs)
            values_manager.append(value_manager)
            values_worker.append(value_worker)
            log_probs.append(log_prob)
            rewards.append(reward)
            intrinsic_rewards.append(intrinsic_reward)

            if done:
                break

        R_worker = torch.zeros(1, 1)
        R_manager = torch.zeros(1, 1)
        if not done:
            value_worker, value_manager, _, _, _, _ = model(obs.unsqueeze(0), states)
            R_worker = value_worker.data
            R_manager = value_manager.data

        values_worker.append(Variable(R_worker))
        values_manager.append(Variable(R_manager))
        policy_loss = 0
        manager_loss = 0
        value_manager_loss = 0
        value_worker_loss = 0
        gae_worker = torch.zeros(1, 1)
        for i in reversed(range(len(rewards))):
            R_worker = gamma_worker * R_worker + rewards[i] + alpha * intrinsic_rewards[i]
            R_manager = gamma_manager * R_manager + rewards[i]
            advantage_worker = R_worker - values_worker[i]
            advantage_manager = R_manager - values_manager[i]
            value_worker_loss = value_worker_loss + 0.5 * advantage_worker.pow(2)
            value_manager_loss = value_manager_loss + 0.5 * advantage_manager.pow(2)

            # Generalized Advantage Estimation
            delta_t_worker = \
                rewards[i] \
                + alpha * intrinsic_rewards[i]\
                + gamma_worker * values_worker[i + 1].data \
                - values_worker[i].data
            gae_worker = gae_worker * gamma_worker * tau_worker + delta_t_worker

            policy_loss = policy_loss \
                - log_probs[i] * gae_worker - entropy_coef * entropies[i]

            if (i + model.c) < len(rewards):
                manager_loss = manager_loss \
                    - advantage_manager * manager_partial_loss[i + model.c]

        optimizer.zero_grad()

        total_loss = policy_loss \
            + manager_loss \
            + value_manager_loss_coef * value_manager_loss \
            + value_worker_loss_coef * value_worker_loss

        total_loss.backward()
        print(
            "Update", epoch,
            "\ttotal_loss :", "%0.2f" % float(total_loss),
            "\tvalue_manager_loss :", "%0.2f" % float(value_manager_loss_coef * value_manager_loss),
            "\tvalue_worker_loss :", "%0.2f" % float(value_worker_loss_coef * value_worker_loss),
            "\tmanager_loss :", "%0.2f" % float(manager_loss),
            "\tpolicy_loss :", "%0.2f" % float(policy_loss)
        )
        plt_loss.add_value(epoch, float(total_loss), "Total loss")
        plt_loss.add_value(epoch, float(value_manager_loss_coef * value_manager_loss), "Value Manager loss")
        plt_loss.add_value(epoch, float(policy_loss), "Policy loss")
        plt_loss.add_value(epoch, float(value_worker_loss_coef * value_worker_loss), "Value Worker loss")
        plt_loss.add_value(epoch, float(manager_loss), "Manager loss")

        plt_loss.draw()

        torch.nn.utils.clip_grad_norm_(model.parameters(), max_grad_norm)

        #ensure_shared_grads(model, shared_model)
        optimizer.step()