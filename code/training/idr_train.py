import os
from datetime import datetime
from pyhocon import ConfigFactory
import sys
import torch

import utils.general as utils
import utils.plots as plt

class IDRTrainRunner():
    def __init__(self,**kwargs):
        torch.set_default_dtype(torch.float32)
        torch.set_num_threads(1)
        self.conf = ConfigFactory.parse_file(kwargs['conf'])
        # 1
        self.batch_size = kwargs['batch_size']
        # 2000
        self.nepochs = kwargs['nepochs']
        # 'exps'
        self.exps_folder_name = kwargs['exps_folder_name']
        # 'ys_fixed_cameras'
        self.expname = self.conf.get_string('train.expname')
        # 9
        scan_id = kwargs['scan_id'] if kwargs['scan_id'] != -1 else self.conf.get_int('dataset.scan_id', default=-1)
        if scan_id != -1:
            # 'ys_fixed_cameras_9'
            self.expname = self.expname + '_{0}'.format(scan_id)
        # True and True 继续上一次的训练
        if kwargs['is_continue'] and kwargs['timestamp'] == 'latest':
            if os.path.exists(os.path.join('../',kwargs['exps_folder_name'],self.expname)):
                timestamps = os.listdir(os.path.join('../',kwargs['exps_folder_name'],self.expname))
                if (len(timestamps)) == 0:
                    is_continue = False
                    timestamp = None
                else:
                    timestamp = sorted(timestamps)[-1]
                    is_continue = True
            else:
                is_continue = False
                timestamp = None
        else:
            # 'latest'
            timestamp = kwargs['timestamp']
            # False
            is_continue = kwargs['is_continue']
        # ../exps
        utils.mkdir_ifnotexists(os.path.join('../',self.exps_folder_name))
        # ../exps/ys_fixed_cameras_9
        self.expdir = os.path.join('../', self.exps_folder_name, self.expname)
        utils.mkdir_ifnotexists(self.expdir)
        self.timestamp = '{:%Y_%m_%d_%H_%M_%S}'.format(datetime.now())
        # ../exps/ys_fixed_cameras_9/2023_04_06
        utils.mkdir_ifnotexists(os.path.join(self.expdir, self.timestamp))
        self.plots_dir = os.path.join(self.expdir, self.timestamp, 'plots')
        utils.mkdir_ifnotexists(self.plots_dir)
        self.checkpoints_path = os.path.join(self.expdir, self.timestamp, 'checkpoints')
        utils.mkdir_ifnotexists(self.checkpoints_path)
        self.model_params_subdir = "ModelParameters"
        self.optimizer_params_subdir = "OptimizerParameters"
        self.scheduler_params_subdir = "SchedulerParameters"
        utils.mkdir_ifnotexists(os.path.join(self.checkpoints_path, self.model_params_subdir))
        utils.mkdir_ifnotexists(os.path.join(self.checkpoints_path, self.optimizer_params_subdir))
        utils.mkdir_ifnotexists(os.path.join(self.checkpoints_path, self.scheduler_params_subdir))
        # 将 conf 信息复制到 runconf.conf 文件
        os.system("""cp -r {0} "{1}" """.format(kwargs['conf'], os.path.join(self.expdir, self.timestamp, 'runconf.conf')))
        
        print('Loading data ...')
        dataset_conf = self.conf.get_config('dataset')
        # 9
        if kwargs['scan_id'] != -1:
            dataset_conf['scan_id'] = kwargs['scan_id']
        # datasets.scene_dataset.SceneDataset(False, dataset_conf)
        self.train_dataset = utils.get_class(self.conf.get_string('train.dataset_class'))(**dataset_conf)
        self.train_dataloader = torch.utils.data.DataLoader(self.train_dataset,
                                                            batch_size=self.batch_size,
                                                            shuffle=True,
                                                            collate_fn=self.train_dataset.collate_fn
                                                            )
        self.plot_dataloader = torch.utils.data.DataLoader(self.train_dataset,
                                                           batch_size=self.conf.get_int('plot.plot_nimgs'),
                                                           shuffle=True,
                                                           collate_fn=self.train_dataset.collate_fn
                                                           )
        print('Finish loading data ...')

        # model.implicit_differentiable_renderer.IDRNetwork(model_conf)
        self.model = utils.get_class(self.conf.get_string('train.model_class'))(conf=self.conf.get_config('model'))
        # True
        if torch.cuda.is_available():
            self.model.cuda()
        # model.loss.IDRLoss(loss_conf)
        self.loss = utils.get_class(self.conf.get_string('train.loss_class'))(**self.conf.get_config('loss'))
        # 1.0e-4
        self.lr = self.conf.get_float('train.learning_rate')
        self.optimizer = torch.optim.Adam(self.model.parameters(), lr=self.lr)
        # [1000,1500]
        self.sched_milestones = self.conf.get_list('train.sched_milestones', default=[])
        # 0.5
        self.sched_factor = self.conf.get_float('train.sched_factor', default=0.0)
        self.scheduler = torch.optim.lr_scheduler.MultiStepLR(self.optimizer, self.sched_milestones, gamma=self.sched_factor)
        
        self.start_epoch = 0
        # True
        if is_continue:
            old_checkpnts_dir = os.path.join(self.expdir, timestamp, 'checkpoints')

            saved_model_state = torch.load(
                os.path.join(old_checkpnts_dir, 'ModelParameters', str(kwargs['checkpoint']) + ".pth"))
            self.model.load_state_dict(saved_model_state["model_state_dict"])
            self.start_epoch = saved_model_state['epoch']

            data = torch.load(
                os.path.join(old_checkpnts_dir, 'OptimizerParameters', str(kwargs['checkpoint']) + ".pth"))
            self.optimizer.load_state_dict(data["optimizer_state_dict"])

            data = torch.load(
                os.path.join(old_checkpnts_dir, self.scheduler_params_subdir, str(kwargs['checkpoint']) + ".pth"))
            self.scheduler.load_state_dict(data["scheduler_state_dict"])

        # 8 张照片
        self.n_batches = len(self.train_dataloader)
        # 100
        self.plot_freq = self.conf.get_int('train.plot_freq')
        self.plot_conf = self.conf.get_config('plot')
        # [250,500,750,1000,1250]
        self.alpha_milestones = self.conf.get_list('train.alpha_milestones', default=[])
        # 2
        self.alpha_factor = self.conf.get_float('train.alpha_factor', default=0.0)
        for acc in self.alpha_milestones:
            if self.start_epoch > acc:
                self.loss.alpha = self.loss.alpha * self.alpha_factor

    def save_checkpoints(self, epoch):
        torch.save(
            {"epoch": epoch, "model_state_dict": self.model.state_dict()},
            os.path.join(self.checkpoints_path, self.model_params_subdir, str(epoch) + ".pth"))
        torch.save(
            {"epoch": epoch, "model_state_dict": self.model.state_dict()},
            os.path.join(self.checkpoints_path, self.model_params_subdir, "latest.pth"))

        torch.save(
            {"epoch": epoch, "optimizer_state_dict": self.optimizer.state_dict()},
            os.path.join(self.checkpoints_path, self.optimizer_params_subdir, str(epoch) + ".pth"))
        torch.save(
            {"epoch": epoch, "optimizer_state_dict": self.optimizer.state_dict()},
            os.path.join(self.checkpoints_path, self.optimizer_params_subdir, "latest.pth"))

        torch.save(
            {"epoch": epoch, "scheduler_state_dict": self.scheduler.state_dict()},
            os.path.join(self.checkpoints_path, self.scheduler_params_subdir, str(epoch) + ".pth"))
        torch.save(
            {"epoch": epoch, "scheduler_state_dict": self.scheduler.state_dict()},
            os.path.join(self.checkpoints_path, self.scheduler_params_subdir, "latest.pth"))

    def run(self):
        
        print("training...")

        # range(0,2000+1)
        for epoch in range(self.start_epoch, self.nepochs + 1):

            # [250,500,750,1000,1250]
            if epoch in self.alpha_milestones:
                # *=2
                self.loss.alpha = self.loss.alpha * self.alpha_factor

            if epoch % 100 == 0:
                self.save_checkpoints(epoch)
            
            # 100 plot
            if epoch % self.plot_freq == 0:
                self.model.eval()
                self.train_dataset.change_sampling_idx(-1)
                indices, model_input, ground_truth = next(iter(self.plot_dataloader))
                model_input["intrinsics"] = model_input["intrinsics"].cuda()
                model_input["uv"] = model_input["uv"].cuda()
                model_input["object_mask"] = model_input["object_mask"].cuda()
                model_input['pose'] = model_input['pose'].cuda()

                split = utils.split_input(model_input, self.total_pixels)
                res = []
                for s in split:
                    out = self.model(s)
                    res.append({
                        'points': out['points'].detach(),
                        'rgb_values': out['rgb_values'].detach(),
                        'network_object_mask': out['network_object_mask'].detach(),
                        'object_mask': out['object_mask'].detach()
                    })

                batch_size = ground_truth['rgb'].shape[0]
                model_outputs = utils.merge_output(res, self.total_pixels, batch_size)

                plt.plot(self.model,
                        indices,
                        model_outputs,
                        model_input['pose'],
                        ground_truth['rgb'],
                        self.plots_dir,
                        epoch,
                        self.img_res,
                        **self.plot_conf
                        )

                self.model.train()
                
            # iteration = 8
            for data_index, (indices, model_input) in enumerate(self.train_dataloader):

                model_input['pose'] = model_input['pose'].cuda()
                model_input["points"] = model_input["points"].cuda()

                # 正向传播
                model_outputs = self.model(model_input)

                # 计算 loss
                loss_output = self.loss(model_outputs, model_input)
                loss = loss_output['loss']
                self.optimizer.zero_grad()
                loss.backward()
                self.optimizer.step()
                

                print('{0} [{1}] ({2}/{3}): loss = {4}, rgb_loss = {5}, eikonal_loss = {6}, mask_loss = {7}, alpha = {8}, lr = {9}'
                    .format(self.expname, epoch, data_index, self.n_batches, loss.item(),
                    loss_output['rgb_loss'].item(),
                    loss_output['eikonal_loss'].item(),
                    loss_output['mask_loss'].item(),
                    self.loss.alpha,
                    self.scheduler.get_lr()[0])
                    )

            self.scheduler.step()
