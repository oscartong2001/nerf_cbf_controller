import argparse
import os
import time

import numpy as np
import torch
import cv2
from tqdm import tqdm
from torch.utils.data import DataLoader
import matplotlib

from src import config
from src.tools.viz import SLAMFrontend
from src.utils.datasets import get_dataset
from src.utils.Renderer import Renderer
from src.NICE_SLAM import NICE_SLAM
from src.common import get_camera_from_tensor
from scipy.spatial.transform import Rotation as R
import matplotlib.pyplot as plt
from matplotlib.animation import FFMpegWriter


with open('device.txt', encoding='utf-8') as file:
     device=file.read()

time_step = 0.1
mu = 0
alpha = 0.5
d = 0.1
u = torch.tensor([[0,0,1]]).t().float().to(device)
max_h = 1.5
intend = sigma = 1
batch_size = 10

def state_to_pose(state):
    for i in range(state.shape[0]):
        rm = torch.from_numpy(R.from_euler('xyz', state[i][3:].cpu(), degrees=True).as_matrix()).to(device)
        pose = torch.cat((torch.cat((rm, state[i][:3].unsqueeze(-1)), 1), torch.tensor([[0,0,0,1]]).to(device)), 0).unsqueeze(0)
        if i == 0:
            batch_pose = pose
        else:
            batch_pose = torch.cat((batch_pose, pose), dim=0)
    return batch_pose

def update_dynamics(state, action):
    return state + time_step * action


class Robot:
    def __init__(self, cfg):
        self.c = np.load('replica_room1.npy',allow_pickle=True).item()
        self.decoders = torch.load('replica_room1.pth')
        self.renderer = NICE_SLAM(cfg, args).renderer
        self.renderer.H = 68
        self.renderer.W = 120
        self.renderer.fx = 60
        self.renderer.fy = 60
        self.renderer.cx = 59.95
        self.renderer.cy = 33.95

    def predict_observation(self, pose):
        depth, _, _ = self.renderer.render_batch_img(
                    self.c,
                    self.decoders,
                    pose,
                    device,
                    stage='middle',
                    gt_depth=None)
        return depth
    
    def render(self, pose):

        self.renderer.H = 680
        self.renderer.W = 1200
        self.renderer.fx = 600.0
        self.renderer.fy = 600.0
        self.renderer.cx = 599.5
        self.renderer.cy = 339.5

        depth, _, _ = self.renderer.render_img(
                    self.c,
                    self.decoders,
                    pose,
                    device,
                    stage='color',
                    gt_depth=None)
        depth, uncertainty, color = self.renderer.render_img(
                    self.c,
                    self.decoders,
                    pose,
                    device,
                    stage='color',
                    gt_depth=depth)
        self.renderer.H = 68
        self.renderer.W = 120
        self.renderer.fx = 60
        self.renderer.fy = 60
        self.renderer.cx = 59.95
        self.renderer.cy = 33.95
        return depth, color



def find_safe_action(robot, pose, h, intended_action, direction):
    state = torch.cat((pose[:3, -1].to(device), torch.from_numpy(R.from_matrix(pose[:3, :3].cpu()).as_euler('xyz', degrees=True)).to(device)), dim=0)
    orient_action = torch.zeros(6).to(device)
    if direction in ['up', 'down']:
        unit = intended_action * torch.mm(pose[:3, :3].float(), u).squeeze().to(device)
        orient_action[0] = unit[0]
        orient_action[1] = unit[1]
        orient_action[2] = unit[2]
    elif direction in ['left', 'right']:
        orient_action[5] = intended_action * 10
    new_state = update_dynamics(state, orient_action).unsqueeze(0)
    new_pose = state_to_pose(new_state)
    new_h = d - robot.predict_observation(new_pose).min().unsqueeze(0)
    best_action = torch.zeros(6).to(device)
    if new_h <= alpha * h:
        print('Intended action {} is safe'.format(orient_action))
        print('intervention = 0')
        return orient_action, new_h, True
    while True:
        batch_action = torch.zeros((batch_size, 6)).to(device)
        if direction in {'up', 'down'}:
            for j in range(batch_size):
                value = np.random.normal(mu, sigma)
                while value * intended_action > 0 and abs(value) >= abs(intended_action):
                    value = np.random.normal(mu, sigma)
                unit = value * torch.mm(pose[:3, :3].float(), u).squeeze().to(device)
                batch_action[j][0] = unit[0]
                batch_action[j][1] = unit[1]
                batch_action[j][2] = unit[2]
        else:
            for j in range(batch_size):
                batch_action[j][5] = np.random.normal(mu, sigma) * 10
        batch_new_state = update_dynamics(state, batch_action)
        batch_new_pose = state_to_pose(batch_new_state)
        batch_new_h = d - robot.predict_observation(batch_new_pose).min(dim=-1)[0].min(dim=-1)[0]
        for j in range(batch_size):
            if batch_new_h[j] <= alpha * h:
                if torch.norm(best_action, p=2) == 0 or torch.norm(batch_action[j] - orient_action, p=2) < torch.norm(best_action - orient_action, p=2):
                    best_action = batch_action[j]
                    new_h = batch_new_h[j]
        if torch.norm(best_action, p=2) > 0:
            print('Intended action {} is unsafe, a recommended substitute is {}'.format(orient_action, best_action))
            print('intervention =', float(torch.norm(orient_action - best_action, p=2)))
            return best_action, new_h, False
    #print('Fail to find a safe action')
    #return best_action, h, False


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Arguments to visualize the SLAM process.'
    )
    parser.add_argument('config', type=str, help='Path to config file.')
    parser.add_argument('--input_folder', type=str,
                        help='input folder, this have higher priority, can overwrite the one in config file')
    parser.add_argument('--output', type=str,
                        help='output folder, this have higher priority, can overwrite the one inconfig file')
    nice_parser = parser.add_mutually_exclusive_group(required=False)
    nice_parser.add_argument('--nice', dest='nice', action='store_true')
    nice_parser.add_argument('--imap', dest='nice', action='store_false')
    parser.set_defaults(nice=True)
    parser.add_argument('--save_rendering',
                        action='store_true', help='save rendering video to `vis.mp4` in output folder ')
    parser.add_argument('--vis_input_frame',
                        action='store_true', help='visualize input frames')
    parser.add_argument('--no_gt_traj',
                        action='store_true', help='not visualize gt trajectory')
    args = parser.parse_args()
    
    cfg = config.load_config(
        args.config, 'configs/nice_slam.yaml' if args.nice else 'configs/imap.yaml')  # args.config: env, nice_slam.yaml: robot
    scale = cfg['scale']
    output = cfg['data']['output'] if args.output is None else args.output

    frame_reader = get_dataset(cfg, args, scale, device=device)
    frame_loader = DataLoader(
            frame_reader, batch_size=1, shuffle=False, num_workers=4)
    ckptsdir = f'{output}/ckpts'
    if os.path.exists(ckptsdir):
        ckpts = [os.path.join(ckptsdir, f)
                 for f in sorted(os.listdir(ckptsdir)) if 'tar' in f]
        if len(ckpts) > 0:
            ckpt_path = ckpts[-1]
            print('Get ckpt :', ckpt_path)
            ckpt = torch.load(ckpt_path, map_location=torch.device(device))
            estimate_c2w_list = ckpt['estimate_c2w_list']
            gt_c2w_list = ckpt['gt_c2w_list']
            N = ckpt['idx']
            estimate_c2w_list[:, :3, 3] /= scale
            gt_c2w_list[:, :3, 3] /= scale
    idx, color, depth, pose = frame_reader[0]

    robot = Robot(cfg)
    fps = 10
    fourcc = cv2.VideoWriter_fourcc(*'MJPG')
    videoWriter = cv2.VideoWriter('video.avi',fourcc,fps,(240,68))
    
    
    pose = state_to_pose(torch.tensor([-2, 0.2,  0.5, 90, 0, 115]).unsqueeze(0).to(device)).squeeze()
    depth, color = robot.render(pose.to(device))
    h = d - depth.min()
    color = cv2.rectangle(color.to(device).detach().cpu().numpy(), (1100, 100), (1150, 580), (0, 0, 0), 10)
    color = cv2.rectangle(color, (1110, min(340-int(240*h/max_h), 340)), (1140, max(340-int(240*h/max_h), 340)), (0, 0, 1), -1)
    color = cv2.putText(color, 'CBF h', (1025, 75), cv2.FONT_HERSHEY_SIMPLEX, 1.75, (0, 0, 0), 5)
    #videoWriter.write(np.hstack([cv2.normalize(depth.unsqueeze(-1).repeat(1,1,3).to(device).detach().cpu().numpy(), dst=None, 
            #alpha=0, beta=255, norm_type=cv2.NORM_MINMAX).astype(np.uint8), (color[:, :, [2,1,0]]*255).astype(np.uint8).clip(0,255)]))
    plt.imshow(np.hstack([cv2.normalize(depth.unsqueeze(-1).repeat(1,1,3).to(device).detach().cpu().numpy(),
            dst=None, alpha=0, beta=1, norm_type=cv2.NORM_MINMAX), color]))
    print('min_depth = {}'.format(depth.min()))
    '''
    plt.xticks([])
    plt.yticks([])
    plt.savefig('t_0.png', bbox_inches='tight', pad_inches=0.0)
    '''
    '''
    cv2.namedWindow("Safety Filter", cv2.WINDOW_KEEPRATIO)
    cv2.imshow('Safety Filter', np.hstack([cv2.normalize(depth.unsqueeze(-1).repeat(1,1,3).to(device).detach().cpu().numpy(),
    dst=None, alpha=0, beta=1, norm_type=cv2.NORM_MINMAX), color[:, :, [2,1,0]]]))
    '''
    '''   
    #while True:
    for i in range(100):
        print('step:', i)
        #k = cv2.waitKeyEx()
        k = 65362
        if k in {65362, 65364, 65361, 65363}:  # up, down, left, right
            start = time.time()
            if k == 65362 :
                intended_action = -intend
                action, h, is_safe = find_safe_action(robot, pose, d - depth.min(), intended_action, 'up')
            elif k == 65364:
                intended_action = intend
                action, h, is_safe = find_safe_action(robot, pose, d - depth.min(), intended_action, 'down')
            elif k == 65361:
                intended_action = intend * 10
                action, h, is_safe = find_safe_action(robot, pose, d - depth.min(), intended_action, 'left')
            else:
                intended_action = -intend * 10
                action, h, is_safe = find_safe_action(robot, pose, d - depth.min(), intended_action, 'right')
            end = time.time()
            state = torch.cat((pose[:3, -1].to(device), torch.from_numpy(R.from_matrix(pose[:3, :3].cpu()).as_euler('xyz', degrees=True)).to(device)), dim=0).to(device)
            state = update_dynamics(state, action)
            pose = state_to_pose(state.unsqueeze(0)).squeeze()
            depth, color = robot.render(pose.to(device))
            if i % 5 == 0:
                color = cv2.rectangle(color.to(device).detach().cpu().numpy(), (1100, 100), (1150, 580), (0, 0, 0), 10)
                if is_safe:
                    color = cv2.rectangle(color, (1110, min(340-int(240*h/max_h), 340)), (1140, max(340-int(240*h/max_h), 340)), (0, 0, 1), -1)
                else:
                    color = cv2.rectangle(color, (1110, min(340-int(240*h/max_h), 340)), (1140, max(340-int(240*h/max_h), 340)), (1, 0, 0), -1)
                cv2.imwrite('{}.png'.format(i), np.hstack([cv2.normalize(depth.unsqueeze(-1).repeat(1,1,3).to(device).detach().cpu().numpy(), dst=None, 
            alpha=0, beta=255, norm_type=cv2.NORM_MINMAX), color[:, :, [2,1,0]]*255]))

            #color = cv2.rectangle(color.to(device).detach().cpu().numpy(), (83, 10), (86, 41), (0, 0, 0), 1)
            #if is_safe:
                #color = cv2.rectangle(color, (84, min(15-int(26*h/max_h), 15)), (85, max(15-int(26*h/max_h), 15)), (0, 0, 1), -1)
            #else:
                #color = cv2.rectangle(color, (84, min(15-int(26*h/max_h), 15)), (85, max(15-int(26*h/max_h), 15)), (1, 0, 0), -1)
            #cv2.imshow('Safety Filter', np.hstack([cv2.normalize(depth.unsqueeze(-1).repeat(1,1,3).to(device).detach().cpu().numpy(),
            #dst=None, alpha=0, beta=1, norm_type=cv2.NORM_MINMAX), color[:, :, [2,1,0]]]))

            print('new state = {}\nnew h = {}\nmin_depth = {}\ntime cost = {}'.format(state, d - depth.min(), depth.min(), end-start))
        elif k == 27:  # esc
            cv2.destroyAllWindows()
            break
    '''
    
    stop_next = -1
    frame = 0
    while frame < 150:
        direction = 0
        if direction == 0 : #up
            frame += 1
            print(frame)
            intended_action = -intend
            action, h, is_safe = find_safe_action(robot, pose, d - depth.min(), intended_action, 'up')
            state = torch.cat((pose[:3, -1].to(device), torch.from_numpy(R.from_matrix(pose[:3, :3].cpu()).as_euler('xyz', degrees=True)).to(device)), dim=0).to(device)
            state = update_dynamics(state, action)
            pose = state_to_pose(state.unsqueeze(0)).squeeze()
            depth, color = robot.render(pose.to(device))
            color = cv2.rectangle(color.to(device).detach().cpu().numpy(), (1100, 100), (1150, 580), (0, 0, 0), 10)
            if is_safe:
                color = cv2.rectangle(color, (1110, min(340-int(240*h/max_h), 340)), (1140, max(340-int(240*h/max_h), 340)), (0, 0, 1), -1)
            else:
                color = cv2.rectangle(color, (1110, min(340-int(240*h/max_h), 340)), (1140, max(340-int(240*h/max_h), 340)), (1, 0, 0), -1)
            color = cv2.putText(color, 'CBF h', (1025, 75), cv2.FONT_HERSHEY_SIMPLEX, 1.75, (0, 0, 0), 5)
            # plt.imshow(np.hstack([cv2.normalize(depth.unsqueeze(-1).repeat(1,1,3).to(device).detach().cpu().numpy(),
            # dst=None, alpha=0, beta=1, norm_type=cv2.NORM_MINMAX), color]))
            # plt.xticks([])
            # plt.yticks([])
            # plt.savefig('t_{}.png'.format(frame), bbox_inches='tight', pad_inches=0.0)
            videoWriter.write(np.hstack([cv2.normalize(depth.unsqueeze(-1).repeat(1,1,3).to(device).detach().cpu().numpy(), dst=None, 
            alpha=0, beta=255, norm_type=cv2.NORM_MINMAX).astype(np.uint8), (color[:, :, [2,1,0]]*255).astype(np.uint8).clip(0,255)]))
            print('min_depth = {}'.format(depth.min()))
        elif direction == 1 and stop_next != 1:  # down
            frame += 1
            print(frame)
            stop_next = -1
            action, h, is_safe = find_safe_action(robot, pose, d - depth.min(), intend, 'down')
            state = torch.cat((pose[:3, -1].to(device), torch.from_numpy(R.from_matrix(pose[:3, :3].cpu()).as_euler('xyz', degrees=True)).to(device)), dim=0).to(device)
            state = update_dynamics(state, action)
            pose = state_to_pose(state.unsqueeze(0)).squeeze()
            depth, color = robot.render(pose.to(device))
            color = cv2.rectangle(color.to(device).detach().cpu().numpy(), (110, 10), (115, 58), (0, 0, 0), 1)
            if is_safe:
                color = cv2.rectangle(color, (111, min(34-int(24*h/max_h), 34)), (114, max(34-int(24*h/max_h), 34)), (0, 0, 1), -1)
            else:
                stop_next = 1
                color = cv2.rectangle(color, (111, min(34-int(24*h/max_h), 34)), (114, max(34-int(24*h/max_h), 34)), (1, 0, 0), -1)
            videoWriter.write(np.hstack([cv2.normalize(depth.unsqueeze(-1).repeat(1,1,3).to(device).detach().cpu().numpy(), dst=None, 
            alpha=0, beta=255, norm_type=cv2.NORM_MINMAX).astype(np.uint8), (color[:, :, [2,1,0]]*255).astype(np.uint8).clip(0,255)]))
            print('min_depth = {}'.format(depth.min()))
        elif direction == 2 and stop_next != 2:  # left
            frame += 1
            print(frame)
            stop_next = -1
            action, h, is_safe = find_safe_action(robot, pose, d - depth.min(), intend, 'left')
            state = torch.cat((pose[:3, -1].to(device), torch.from_numpy(R.from_matrix(pose[:3, :3].cpu()).as_euler('xyz', degrees=True)).to(device)), dim=0).to(device)
            state = update_dynamics(state, action)
            pose = state_to_pose(state.unsqueeze(0)).squeeze()
            depth, color = robot.render(pose.to(device))
            color = cv2.rectangle(color.to(device).detach().cpu().numpy(), (110, 10), (115, 58), (0, 0, 0), 1)
            if is_safe:
                color = cv2.rectangle(color, (111, min(34-int(24*h/max_h), 34)), (114, max(34-int(24*h/max_h), 34)), (0, 0, 1), -1)
            else:
                stop_next = 2
                color = cv2.rectangle(color, (111, min(34-int(24*h/max_h), 34)), (114, max(34-int(24*h/max_h), 34)), (1, 0, 0), -1)
            videoWriter.write(np.hstack([cv2.normalize(depth.unsqueeze(-1).repeat(1,1,3).to(device).detach().cpu().numpy(), dst=None, 
            alpha=0, beta=255, norm_type=cv2.NORM_MINMAX).astype(np.uint8), (color[:, :, [2,1,0]]*255).astype(np.uint8).clip(0,255)]))
            print('min_depth = {}'.format(depth.min()))
        elif direction == 3 and stop_next != 3:  # right
            frame += 1
            print(frame)
            stop_next = -1
            action, h, is_safe = find_safe_action(robot, pose, d - depth.min(), -intend, 'right')
            state = torch.cat((pose[:3, -1].to(device), torch.from_numpy(R.from_matrix(pose[:3, :3].cpu()).as_euler('xyz', degrees=True)).to(device)), dim=0).to(device)
            state = update_dynamics(state, action)
            pose = state_to_pose(state.unsqueeze(0)).squeeze()
            depth, color = robot.render(pose.to(device))
            color = cv2.rectangle(color.to(device).detach().cpu().numpy(), (110, 10), (115, 58), (0, 0, 0), 1)
            if is_safe:
                color = cv2.rectangle(color, (111, min(34-int(24*h/max_h), 34)), (114, max(34-int(24*h/max_h), 34)), (0, 0, 1), -1)
            else:
                stop_next = 3
                color = cv2.rectangle(color, (111, min(34-int(24*h/max_h), 34)), (114, max(34-int(24*h/max_h), 34)), (1, 0, 0), -1)
            videoWriter.write(np.hstack([cv2.normalize(depth.unsqueeze(-1).repeat(1,1,3).to(device).detach().cpu().numpy(), dst=None, 
            alpha=0, beta=255, norm_type=cv2.NORM_MINMAX).astype(np.uint8), (color[:, :, [2,1,0]]*255).astype(np.uint8).clip(0,255)]))
            print('min_depth = {}'.format(depth.min()))

    videoWriter.release()
     
