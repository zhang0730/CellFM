from torch_geometric.data import Data
import torch
import numpy as np
import pickle
from torch_geometric.data import DataLoader
import os
import scanpy as sc
import networkx as nx
from tqdm import tqdm
import pandas as pd

import warnings
warnings.filterwarnings("ignore")
sc.settings.verbosity = 0

from .data_utils import get_DE_genes, get_dropout_non_zero_genes, DataSplitter
from .utils import print_sys, zip_data_download_wrapper, dataverse_download, filter_pert_in_go

#PertData 是一个用于处理单细胞扰动实验数据的 Python 类，主要用于构建图神经网络(GNN)所需的图数据结构。

#主要功能
#1.数据加载与预处理：支持从多种来源加载单细胞转录组数据，包括公开数据集(Norman, Adamson, Dixit)或自定义h5ad文件。
#2.数据分割：提供多种数据分割策略，用于训练、验证和测试模型。
#3.图结构构建：将单细胞数据转换为图神经网络所需的图数据结构(PyG Data对象)。
#4.数据加载器创建：生成PyTorch Geometric的DataLoader，便于批量训练。

class PertData:
    
    def __init__(self, data_path, gi_go = False, gene_path = None):
        self.data_path = data_path
        if not os.path.exists(self.data_path):
            os.mkdir(self.data_path) #设置数据存储路径
        server_path = 'https://dataverse.harvard.edu/api/access/datafile/6153417'
        # dataverse_download(server_path, os.path.join(self.data_path, 'gene2go_all.pkl'))
        print('read',os.path.join(self.data_path, 'gene2go.pkl'))
        with open(os.path.join(self.data_path, 'gene2go.pkl'), 'rb') as f:  #加载基因到GO(Gene Ontology)的映射关系
            gene2go = pickle.load(f)
        
        self.gi_go = gi_go
        # if gene_path is not None:
        #     gene_path = gene_path
        # elif self.gi_go:
        #     gene_path = '/dfs/user/kexinh/gears2/data/pert_genes_gi.pkl'
        # else:
        #     gene_path = '/dfs/user/kexinh/gears2/data/essential_all_data_pert_genes.pkl'
        # with open(gene_path, 'rb') as f:
        #     essential_genes = pickle.load(f)
    
        # gene2go = {i: gene2go[i] for i in essential_genes if i in gene2go}

        self.pert_names = np.unique(list(gene2go.keys())) #初始化扰动名称列表和映射关系
        self.node_map_pert = {x: it for it, x in enumerate(self.pert_names)}
            
    def load(self, data_name = None, 
             data_path = None):
        if data_name in ['norman', 'adamson', 'dixit']: #支持三种公开数据集或自定义h5ad文件，注意这里的是在配置那边自己设置的
            ## load from harvard dataverse
            if data_name == 'norman':
                url = 'https://dataverse.harvard.edu/api/access/datafile/6154020'
            elif data_name == 'adamson':
                url = 'https://dataverse.harvard.edu/api/access/datafile/6154417'
            elif data_name == 'dixit':
                url = 'https://dataverse.harvard.edu/api/access/datafile/6154416'
            data_path = os.path.join(self.data_path, data_name)
            print('downloading data')
            zip_data_download_wrapper(url, data_path, self.data_path)            
            self.dataset_name = data_path.split('/')[-1]
            self.dataset_path = data_path
            adata_path = os.path.join(data_path, 'perturb_processed.h5ad')
            self.adata = sc.read_h5ad(adata_path)
            self.adata.obs_names_make_unique()

        elif os.path.exists(data_path):
            print(data_path)
            adata_path = os.path.join(data_path, 'perturb_processed.h5ad')
            self.adata = sc.read_h5ad(adata_path)
            print(self.adata.shape)
            self.adata.obs_names_make_unique()
            self.dataset_name = data_path.split('/')[-1]
            self.dataset_path = data_path
        else:
            raise ValueError("data is either Norman/Adamson/Dixit or a path to an h5ad file") #判断输入数据在不在
        
        print_sys('These perturbations are not in the GO graph and is thus not able to make prediction for...')
        not_in_go_pert = np.array(self.adata.obs[self.adata.obs.condition.apply(lambda x: not filter_pert_in_go(x, self.pert_names))].condition.unique())
        print_sys(not_in_go_pert)
        
        filter_go = self.adata.obs[self.adata.obs.condition.apply(lambda x: filter_pert_in_go(x, self.pert_names))]  #处理数据确保扰动在GO图中存在
        self.adata = self.adata[filter_go.index.values, :]
        pyg_path = os.path.join(data_path, 'data_pyg')
        if not os.path.exists(pyg_path):
            os.mkdir(pyg_path)
        dataset_fname = os.path.join(pyg_path, 'cell_graphs.pkl')
                
        if os.path.isfile(dataset_fname):
            print_sys("Local copy of pyg dataset is detected. Loading...")
            self.dataset_processed = pickle.load(open(dataset_fname, "rb"))        
            print_sys("Done!")
        else:
            self.ctrl_adata = self.adata[self.adata.obs['condition'] == 'ctrl']
            self.gene_names = self.adata.var.gene_name
            
            
            print_sys("Creating pyg object for each cell in the data...") #创建或加载预处理好的图数据
            self.dataset_processed = self.create_dataset_file()
            print_sys("Saving new dataset pyg object at " + dataset_fname) 
            pickle.dump(self.dataset_processed, open(dataset_fname, "wb"))    
            print_sys("Done!")
            
    def new_data_process(self, dataset_name,
                         adata = None,
                         skip_calc_de = False): #处理新的单细胞数据集
        
        if 'condition' not in adata.obs.columns.values:
            raise ValueError("Please specify condition")
        if 'gene_name' not in adata.var.columns.values:
            raise ValueError("Please specify gene name")
        if 'cell_type' not in adata.obs.columns.values:
            raise ValueError("Please specify cell type")
        
        dataset_name = dataset_name.lower()
        self.dataset_name = dataset_name
        save_data_folder = os.path.join(self.data_path, dataset_name)
        
        if not os.path.exists(save_data_folder):
            os.mkdir(save_data_folder)
        self.dataset_path = save_data_folder
        self.adata = get_DE_genes(adata, skip_calc_de) #计算差异表达基因
        if not skip_calc_de:
            self.adata = get_dropout_non_zero_genes(self.adata)
        self.adata.write_h5ad(os.path.join(save_data_folder, 'perturb_processed.h5ad'))
        
        self.ctrl_adata = self.adata[self.adata.obs['condition'] == 'ctrl']
        self.gene_names = self.adata.var.gene_name
        pyg_path = os.path.join(save_data_folder, 'data_pyg')
        if not os.path.exists(pyg_path):
            os.mkdir(pyg_path)
        dataset_fname = os.path.join(pyg_path, 'cell_graphs.pkl')
        print_sys("Creating pyg object for each cell in the data...")
        self.dataset_processed = self.create_dataset_file() #保存处理后的数据
        print_sys("Saving new dataset pyg object at " + dataset_fname) 
        pickle.dump(self.dataset_processed, open(dataset_fname, "wb"))    
        print_sys("Done!")
        
    def prepare_split(self, split = 'simulation', 
                      seed = 1, 
                      train_gene_set_size = 0.75,
                      combo_seen2_train_frac = 0.75,
                      combo_single_split_test_set_fraction = 0.1,
                      test_perts = None,
                      only_test_set_perts = False,
                      test_pert_genes = None,
                      split_dict_path=None): #数据分割
        available_splits = ['simulation', 'simulation_single', 'combo_seen0', 'combo_seen1', 
                            'combo_seen2', 'single', 'no_test', 'no_split', 'custom'] #提供多种分割策略：
        if split not in available_splits:
            raise ValueError('currently, we only support ' + ','.join(available_splits))
        self.split = split
        self.seed = seed
        self.subgroup = None
        self.train_gene_set_size = train_gene_set_size

        if split == 'custom':
            try:
                with open(split_dict_path, 'rb') as f:
                    self.set2conditions = pickle.load(f)
            except:
                    raise ValueError('Please set split_dict_path for custom split')
            return
        
        split_folder = os.path.join(self.dataset_path, 'splits')
        if not os.path.exists(split_folder):
            os.mkdir(split_folder)
        split_file = self.dataset_name + '_' + split + '_' + str(seed) + '_' + str(train_gene_set_size) + '.pkl'
        split_path = os.path.join(split_folder, split_file)
        
        if test_perts:
            split_path = split_path[:-4] + '_' + test_perts + '.pkl'
        
        if os.path.exists(split_path):
            print_sys("Local copy of split is detected. Loading...")
            set2conditions = pickle.load(open(split_path, "rb"))
            if split == 'simulation':
                subgroup_path = split_path[:-4] + '_subgroup.pkl'
                subgroup = pickle.load(open(subgroup_path, "rb"))
                self.subgroup = subgroup
        else:
            print_sys("Creating new splits....")
            if test_perts:
                test_perts = test_perts.split('_')
                    
            if split in ['simulation', 'simulation_single']:
                DS = DataSplitter(self.adata, split_type=split)
                
                adata, subgroup = DS.split_data(train_gene_set_size = train_gene_set_size, 
                                                combo_seen2_train_frac = combo_seen2_train_frac,
                                                seed=seed,
                                                test_perts = test_perts,
                                                only_test_set_perts = only_test_set_perts
                                               )
                subgroup_path = split_path[:-4] + '_subgroup.pkl'
                pickle.dump(subgroup, open(subgroup_path, "wb"))
                self.subgroup = subgroup
                
            elif split[:5] == 'combo':
                split_type = 'combo'
                seen = int(split[-1])

                if test_pert_genes:
                    test_pert_genes = test_pert_genes.split('_')
                
                DS = DataSplitter(self.adata, split_type=split_type, seen=int(seen))
                adata = DS.split_data(test_size=combo_single_split_test_set_fraction,
                                      test_perts=test_perts,
                                      test_pert_genes=test_pert_genes,
                                      seed=seed)
            
            elif split == 'single':
                DS = DataSplitter(self.adata, split_type=split)
                adata = DS.split_data(test_size=combo_single_split_test_set_fraction, seed=seed)
            
            elif split == 'no_test':
                DS = DataSplitter(self.adata, split_type=split)
                adata = DS.split_data(test_size=combo_single_split_test_set_fraction, seed=seed)
            
            elif split == 'no_split':          
                adata = self.adata
                adata.obs['split'] = 'test'
            
            set2conditions = dict(adata.obs.groupby('split').agg({'condition': lambda x: x}).condition)
            set2conditions = {i: j.unique().tolist() for i,j in set2conditions.items()} 
            pickle.dump(set2conditions, open(split_path, "wb"))
            print_sys("Saving new splits at " + split_path)
            
        self.set2conditions = set2conditions

        if split == 'simulation':
            print_sys('Simulation split test composition:')
            for i,j in subgroup['test_subgroup'].items():
                print_sys(i + ':' + str(len(j)))
        print_sys("Done!")
        
    def get_dataloader(self, batch_size, test_batch_size = None): #数据加载器创建
        if test_batch_size is None:
            test_batch_size = batch_size #支持不同批量大小设置
            
        self.node_map = {x: it for it, x in enumerate(self.adata.var.gene_name)}
        self.gene_names = self.adata.var.gene_name
       
        # Create cell graphs 首先创建细胞图：每个细胞表示为一个图，节点是基因，边由先验知识(如GO)构建。
        cell_graphs = {}
        if self.split == 'no_split':
            i = 'test'
            cell_graphs[i] = []
            for p in self.set2conditions[i]:
                if p != 'ctrl':
                    cell_graphs[i].extend(self.dataset_processed[p])
                
            print_sys("Creating dataloaders....")
            # Set up dataloaders
            test_loader = DataLoader(cell_graphs['test'],
                                batch_size=batch_size, shuffle=False)

            print_sys("Dataloaders created...")
            return {'test_loader': test_loader}
        else:
            if self.split =='no_test':
                splits = ['train','val']
            else:
                splits = ['train','val','test']
            for i in splits:
                cell_graphs[i] = []
                for p in self.set2conditions[i]:
                    cell_graphs[i].extend(self.dataset_processed[p])

            print_sys("Creating dataloaders....")
            
            # Set up dataloaders  根据分割创建训练、验证和测试集的DataLoader
            train_loader = DataLoader(cell_graphs['train'],
                                batch_size=batch_size, shuffle=True, drop_last = True)
            val_loader = DataLoader(cell_graphs['val'],
                                batch_size=batch_size, shuffle=True)
            
            if self.split !='no_test':
                test_loader = DataLoader(cell_graphs['test'],
                                batch_size=batch_size, shuffle=False)
                self.dataloader =  {'train_loader': train_loader,
                                    'val_loader': val_loader,
                                    'test_loader': test_loader}

            else: 
                self.dataloader =  {'train_loader': train_loader,
                                    'val_loader': val_loader}
            print_sys("Done!")
        #del self.dataset_processed # clean up some memory
    
        
    def create_dataset_file(self):
        dl = {}
        for p in tqdm(self.adata.obs['condition'].unique()):
            cell_graph_dataset = self.create_cell_graph_dataset(self.adata, p, num_samples=1)
            dl[p] = cell_graph_dataset
        return dl
    
    def get_pert_idx(self, pert_category, adata_): #通过pert_idx标记细胞受到的扰动。
        try:
            pert_idx = [np.where(p == self.pert_names)[0][0]
                    for p in pert_category.split('+')
                    if p != 'ctrl']
        except:
            print(pert_category)
            pert_idx = None
            
        return pert_idx

    # Set up feature matrix and output
        
    def create_cell_graph(self, X, z, y, de_idx, pert, pert_idx=None): #图数据创建 真的是新颖的想法 牛逼

        #pert_feats = np.expand_dims(pert_feats, 0)
        #feature_mat = torch.Tensor(np.concatenate([X, pert_feats])).T
        feature_mat = torch.Tensor(X).T
        emb_mat = torch.tensor([z], dtype=torch.int64)
        
        #将单细胞数据转换为图结构 怎么做到的呢 搞不懂
        '''
        pert_feats = np.zeros(len(self.pert_names))
        if pert_idx is not None:
            for p in pert_idx:
                pert_feats[int(np.abs(p))] = 1
        pert_feats = torch.Tensor(pert_feats).T
        '''
        if pert_idx is None:
            pert_idx = [-1]
        return Data(x=feature_mat, z=emb_mat, pert_idx=pert_idx,
                    y=torch.Tensor(y), de_idx=de_idx, pert=pert)

    def create_cell_graph_dataset(self, split_adata, pert_category,
                                  num_samples=1):
        """
        Combine cell graphs to create a dataset of cell graphs 这次是多个细胞的了 成图
        """
        #包含特征矩阵、扰动索引、差异表达基因等信息
        num_de_genes = 20        
        adata_ = split_adata[split_adata.obs['condition'] == pert_category]
        if 'rank_genes_groups_cov_all' in adata_.uns:
            de_genes = adata_.uns['rank_genes_groups_cov_all']
            de = True
        else:
            de = False
            num_de_genes = 1
        Xs = []
        zs = []
        ys = []
        #处理控制组和实验组的差异
        # When considering a non-control perturbation
        if pert_category != 'ctrl':
            # Get the indices of applied perturbation
            pert_idx = self.get_pert_idx(pert_category, adata_)

            # Store list of genes that are most differentially expressed for testing
            pert_de_category = adata_.obs['condition_name'][0]
            if de:
                de_idx = np.where(adata_.var_names.isin(
                np.array(de_genes[pert_de_category][:num_de_genes])))[0]
            else:
                de_idx = [-1] * num_de_genes
            for cell_z in adata_.X:
                # Use samples from control as basal expression
                sample_index = np.random.randint(0, len(self.ctrl_adata), num_samples)
                ctrl_samples = self.ctrl_adata[sample_index, :]
                if 'total_count' in ctrl_samples.obs:
                    ctrl_obs_counts = ctrl_samples.obs['total_count']
                else:
                    ctrl_obs_counts = ctrl_samples.X.todense().sum(1)
                for ic, c in enumerate(ctrl_samples.X):
                    ipert_total_count = np.array([[float(ctrl_obs_counts[ic])]])
                    comb = np.append(c.toarray(), ipert_total_count,axis=1)
                    Xs.append(comb)
                    ys.append(cell_z)
                    zs.append(sample_index[ic])

        # When considering a control perturbation 实验组细胞与控制组细胞配对，用于学习扰动效应。
        else:
            pert_idx = None
            de_idx = [-1] * num_de_genes
            if 'total_count' in adata_.obs:
                ctrl_obs_counts = adata_.obs['total_count']
            else:
                ctrl_obs_counts = adata_.X.todense().sum(1)
            for ic, cell_z in enumerate(adata_.X):
                ipert_total_count = np.array([[float(ctrl_obs_counts[ic])]])
                comb = np.append(cell_z.toarray(), ipert_total_count,axis=1)
                Xs.append(comb)
                ys.append(cell_z)
            zs = np.arange(0, len(adata_))

        # Create cell graphs
        cell_graphs = []
        for X, z, y in zip(Xs, zs, ys):
            cell_graphs.append(self.create_cell_graph(X, z,
                                y.toarray(), de_idx, pert_category, pert_idx))

        return cell_graphs
    
#该类主要用于构建单细胞扰动响应预测的图神经网络，特别适合：
#预测基因扰动后的表达变化
#研究组合扰动的协同效应
#探索基因调控网络