# -*- coding:utf-8 -*-
import os
import torch 
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
import torchvision.transforms as transforms
from data.cifar import CIFAR10, CIFAR100
from data.mnist import MNIST
from model import CNN
import argparse, sys
import numpy as np
import datetime
import shutil

from loss import loss_coteaching
from actorcritic import Actor, Critic

parser = argparse.ArgumentParser()
parser.add_argument('--lr', type = float, default = 0.1)
parser.add_argument('--actor_lr', type = float, default = 0.1)
parser.add_argument('--critic_lr', type = float, default = 0.1)
parser.add_argument('--result_dir', type = str, help = 'dir to save result txt files', default = 'results/')
parser.add_argument('--noise_rate', type = float, help = 'corruption rate, should be less than 1', default = 0.2)
parser.add_argument('--forget_rate', type = float, help = 'forget rate', default = None)
parser.add_argument('--noise_type', type = str, help='[pairflip, symmetric]', default='pairflip')
parser.add_argument('--top_bn', action='store_true')
parser.add_argument('--dataset', type = str, help = 'mnist, cifar10, or cifar100', default = 'mnist')
parser.add_argument('--n_epoch', type=int, default=200)
parser.add_argument('--train_epoch', type=int, default=20)
parser.add_argument('--seed', type=int, default=1)
parser.add_argument('--print_freq', type=int, default=50)
parser.add_argument('--num_workers', type=int, default=4, help='how many subprocesses to use for data loading')
parser.add_argument('--num_iter_per_epoch', type=int, default=400)
parser.add_argument('--epoch_decay_start', type=int, default=200)

args = parser.parse_args()

# Seed
torch.manual_seed(args.seed)
torch.cuda.manual_seed(args.seed)

# Hyper Parameters
batch_size = 128
learning_rate = args.lr 

# load dataset
if args.dataset=='mnist':
    input_channel=1
    num_classes=10
    args.top_bn = False
    args.epoch_decay_start = 80
    args.n_epoch = 200
    train_dataset = MNIST(root='./data/',
                                download=True,  
                                train=True, 
                                transform=transforms.ToTensor(),
                                noise_type=args.noise_type,
                                noise_rate=args.noise_rate
                         )
    
    test_dataset = MNIST(root='./data/',
                               download=True,  
                               train=False, 
                               transform=transforms.ToTensor(),
                               noise_type=args.noise_type,
                               noise_rate=args.noise_rate
                        )
    
if args.dataset=='cifar10':
    input_channel=3
    num_classes=10
    args.top_bn = False
    # args.epoch_decay_start = 80
    args.epoch_decay_start = 200
    args.n_epoch = 200
    train_dataset = CIFAR10(root='./data/',
                                download=True,  
                                train=True, 
                                transform=transforms.ToTensor(),
                                noise_type=args.noise_type,
                                noise_rate=args.noise_rate
                           )
    
    test_dataset = CIFAR10(root='./data/',
                                download=True,  
                                train=False, 
                                transform=transforms.ToTensor(),
                                noise_type=args.noise_type,
                                noise_rate=args.noise_rate
                          )

if args.dataset=='cifar100':
    input_channel=3
    num_classes=100
    args.top_bn = False
    args.epoch_decay_start = 100
    args.n_epoch = 200
    train_dataset = CIFAR100(root='./data/',
                                download=True,  
                                train=True, 
                                transform=transforms.ToTensor(),
                                noise_type=args.noise_type,
                                noise_rate=args.noise_rate
                            )
    
    test_dataset = CIFAR100(root='./data/',
                                download=True,  
                                train=False, 
                                transform=transforms.ToTensor(),
                                noise_type=args.noise_type,
                                noise_rate=args.noise_rate
                            )

if args.forget_rate is None:
    forget_rate=args.noise_rate
else:
    forget_rate=args.forget_rate

noise_or_not = train_dataset.noise_or_not

# Adjust learning rate and betas for Adam Optimizer
mom1 = 0.9
# mom2 = 0.1
alpha_plan=np.ones(args.n_epoch,dtype=float)*learning_rate
alpha_plan[:int(args.n_epoch*0.5)] = [learning_rate] * int(args.n_epoch*0.5)
alpha_plan[int(args.n_epoch*0.5):int(args.n_epoch*0.75)] = [learning_rate*0.1] * int(args.n_epoch*0.25)
alpha_plan[int(args.n_epoch*0.75):] = [learning_rate*0.01] * int(args.n_epoch*0.25)
beta1_plan = [mom1] * args.n_epoch
# for i in range(args.epoch_decay_start, args.n_epoch):
    # alpha_plan[i] = float(args.n_epoch - i) / (args.n_epoch - args.epoch_decay_start) * learning_rate
    # beta1_plan[i] = mom2

def adjust_learning_rate(optimizer, epoch):
    for param_group in optimizer.param_groups:
        param_group['lr']=alpha_plan[epoch]
        param_group['momentum']=beta1_plan[epoch] # Only change beta1
        
save_dir = args.result_dir +'/' +args.dataset+'/'

if not os.path.exists(save_dir):
    os.system('mkdir -p %s' % save_dir)

nowTime=datetime.datetime.now().strftime('%Y-%m-%d-%H:%M:%S')
model_str=args.dataset+'_rl_coteaching_'+args.noise_type+'_'+str(args.noise_rate)+("-%s.txt" % nowTime)
txtfile=save_dir+"/"+model_str

# Data Loader (Input Pipeline)
print('loading dataset...')
train_loader = torch.utils.data.DataLoader(dataset=train_dataset,
                                           batch_size=batch_size, 
                                           num_workers=args.num_workers,
                                           drop_last=True,
                                           shuffle=True)
    
test_loader = torch.utils.data.DataLoader(dataset=test_dataset,
                                          batch_size=batch_size, 
                                          num_workers=args.num_workers,
                                          drop_last=True,
                                          shuffle=False)

def accuracy(logit, target, topk=(1,)):
    """Computes the precision@k for the specified values of k"""
    output = F.softmax(logit, dim=1)
    maxk = max(topk)
    batch_size = target.size(0)

    _, pred = output.topk(maxk, 1, True, True)
    pred = pred.t()
    correct = pred.eq(target.view(1, -1).expand_as(pred))

    res = []
    for k in topk:
        correct_k = correct[:k].view(-1).float().sum(0, keepdim=True)
        res.append(correct_k.mul_(100.0 / batch_size))
    return res

# Train the Model
def train(train_loader,epoch, model1, optimizer1, model2, optimizer2, rate_schedule):
    print('Training %s...' % model_str)
    pure_ratio_list=[]
    pure_ratio_1_list=[]
    pure_ratio_2_list=[]
    
    train_total=0
    train_correct=0 
    train_total2=0
    train_correct2=0 

    for i, (images, labels, indexes) in enumerate(train_loader):
        ind=indexes.cpu().numpy().transpose()
        if i>args.num_iter_per_epoch:
            break
      
        images = Variable(images).cuda()
        labels = Variable(labels).cuda()
        
        # Forward + Backward + Optimize
        logits1=model1(images)
        prec1, _ = accuracy(logits1, labels, topk=(1, 5))
        train_total+=1
        train_correct+=prec1

        logits2 = model2(images)
        prec2, _ = accuracy(logits2, labels, topk=(1, 5))
        train_total2+=1
        train_correct2+=prec2
        rate_schedule[epoch]=min(rate_schedule[epoch],0.99)
        loss_1, loss_2, pure_ratio_1, pure_ratio_2 = loss_coteaching(logits1, logits2, labels, rate_schedule[epoch], ind, noise_or_not)
        pure_ratio_1_list.append(100*pure_ratio_1)
        pure_ratio_2_list.append(100*pure_ratio_2)

        optimizer1.zero_grad()
        loss_1.backward()
        optimizer1.step()
        optimizer2.zero_grad()
        loss_2.backward()
        optimizer2.step()
        if (i+1) % args.print_freq == 0:
            print ('Epoch [%d/%d], Iter [%d/%d] Training Accuracy1: %.4f, Training Accuracy2: %.4f, Loss1: %.4f, Loss2: %.4f, Pure Ratio1: %.4f, Pure Ratio2 %.4f' 
                  %(epoch+1, args.n_epoch, i+1, len(train_dataset)//batch_size, prec1, prec2, loss_1.data[0], loss_2.data[0], np.sum(pure_ratio_1_list)/len(pure_ratio_1_list), np.sum(pure_ratio_2_list)/len(pure_ratio_2_list)))

    train_acc1=float(train_correct)/float(train_total)
    train_acc2=float(train_correct2)/float(train_total2)
    return train_acc1, train_acc2, pure_ratio_1_list, pure_ratio_2_list

# Evaluate the Model
def evaluate(test_loader, model1, model2):
    print('Evaluating %s...' % model_str)
    model1.eval()    # Change model to 'eval' mode.
    correct1 = 0
    total1 = 0
    for images, labels, _ in test_loader:
        images = Variable(images).cuda()
        logits1 = model1(images)
        outputs1 = F.softmax(logits1, dim=1)
        _, pred1 = torch.max(outputs1.data, 1)
        total1 += labels.size(0)
        correct1 += (pred1.cpu() == labels).sum()

    model2.eval()    # Change model to 'eval' mode 
    correct2 = 0
    total2 = 0
    for images, labels, _ in test_loader:
        images = Variable(images).cuda()
        logits2 = model2(images)
        outputs2 = F.softmax(logits2, dim=1)
        _, pred2 = torch.max(outputs2.data, 1)
        total2 += labels.size(0)
        correct2 += (pred2.cpu() == labels).sum()
 
    acc1 = 100*float(correct1)/float(total1)
    acc2 = 100*float(correct2)/float(total2)
    return acc1, acc2

def getQTarget(actoragent, criticagent, nextStateBatch, rewardBatch, terminalBatch, discount):
    targetBatch = torch.FloatTensor(rewardBatch).cuda()
    terminalBatch = np.asarray(terminalBatch==0,dtype=np.uint8)
    nonFinalMask = torch.ByteTensor(terminalBatch)
    nextStateBatch = torch.FloatTensor(nextStateBatch)
    nextStateBatch = Variable(nextStateBatch).cuda()
    nextActionBatch = actoragent(nextStateBatch)
    qNext = criticagent(nextStateBatch, nextActionBatch)
    nonFinalMask = discount * nonFinalMask.type(torch.cuda.FloatTensor)
    targetBatch += nonFinalMask * qNext.squeeze().data
    targetBatch = torch.reshape(targetBatch,(4,1))
    return Variable(targetBatch, volatile=False)

def getMaxAction(Actor, curState):
    noise = np.random.rand()*0.01
    action = Actor(curState)
    actionnoise = action + noise
    # print(actionnoise)
    return float(actionnoise)

def main():

    rate_schedule = np.ones(args.n_epoch)*0.5
    rate_schedule[:10] = args.noise_rate*np.arange(10)/10
    split_points = [0.05, 0.16, 0.4, 1]
    '''
    mean_pure_ratio1=0
    mean_pure_ratio2=0

    print('building model...')
    cnn1 = CNN(input_channel=input_channel, n_outputs=num_classes)
    cnn1.cuda()
    print(cnn1.parameters)
    optimizer1 = torch.optim.SGD(cnn1.parameters(), lr=learning_rate)
    
    cnn2 = CNN(input_channel=input_channel, n_outputs=num_classes)
    cnn2.cuda()
    print(cnn2.parameters)
    optimizer2 = torch.optim.SGD(cnn2.parameters(), lr=learning_rate)
    
    epoch=0
    train_acc1=0
    train_acc2=0
    # evaluate models with random weights
    test_acc1, test_acc2=evaluate(test_loader, cnn1, cnn2)
    print('Epoch [%d/%d] Test Accuracy on the %s test images: Model1 %.4f %% Model2 %.4f %% Pure Ratio1 %.4f %% Pure Ratio2 %.4f %%' % (epoch+1, args.n_epoch, len(test_dataset), test_acc1, test_acc2, mean_pure_ratio1, mean_pure_ratio2))
    prev_acc = (test_acc1+test_acc2)/200
    prev_rt = rate_schedule[epoch]
    train_batch = np.zeros(5,dtype=object)
    # save results
    with open(txtfile, "a") as myfile:
        myfile.write(str(int(epoch)) + ' '  + str(train_acc1) +' '  + str(train_acc2) +' '  + str(test_acc1) + " " + str(test_acc2) + ' '  + str(mean_pure_ratio1) + ' '  + str(mean_pure_ratio2) + ' ' + str(rate_schedule[epoch]) + "\n")

    for epoch in range(1, args.n_epoch):
        # train models
        cnn1.train()
        adjust_learning_rate(optimizer1, epoch)
        cnn2.train()
        adjust_learning_rate(optimizer2, epoch)
        train_acc1, train_acc2, pure_ratio_1_list, pure_ratio_2_list=train(train_loader, epoch, cnn1, optimizer1, cnn2, optimizer2, rate_schedule)
        # evaluate models
        test_acc1, test_acc2=evaluate(test_loader, cnn1, cnn2)
        # save results
        mean_pure_ratio1 = sum(pure_ratio_1_list)/len(pure_ratio_1_list)
        mean_pure_ratio2 = sum(pure_ratio_2_list)/len(pure_ratio_2_list)
        print('Epoch [%d/%d] Test Accuracy on the %s test images: Model1 %.4f %% Model2 %.4f %%, Pure Ratio 1 %.4f %%, Pure Ratio 2 %.4f %%' % (epoch+1, args.n_epoch, len(test_dataset), test_acc1, test_acc2, mean_pure_ratio1, mean_pure_ratio2))
        with open(txtfile, "a") as myfile:
            myfile.write(str(int(epoch)) + ': '  + str(train_acc1) +' '  + str(train_acc2) +' '  + str(test_acc1) + " " + str(test_acc2) + ' '  + str(mean_pure_ratio1) + ' '  + str(mean_pure_ratio2) + ' ' + str(rate_schedule[epoch]) + "\n")
        if epoch==int(args.n_epoch*split_points[0])-1 or epoch==int(args.n_epoch*split_points[1])-1 or epoch==int(args.n_epoch*split_points[2])-1 or epoch==args.n_epoch-1:
            if epoch==int(args.n_epoch*split_points[0])-1:
                train_batch[0]=np.asarray([prev_acc,prev_rt])
                train_batch[1]=args.noise_rate*20
                train_batch[2]=np.asarray([(test_acc1+test_acc2)/200,rate_schedule[epoch]])
                prev_acc=train_batch[2][0]
                prev_rt=train_batch[2][1]
                train_batch[3]=train_batch[2][0]-train_batch[0][0]
                train_batch[4]=0
                train_batch=train_batch[np.newaxis, :]
            else:
                train_batch = np.append(train_batch, np.asarray([[ [prev_acc,prev_rt], 0, [(test_acc1+test_acc2)/200,rate_schedule[epoch]], (test_acc1+test_acc2)/200-prev_acc, 0]]), axis=0)
                prev_acc=(test_acc1+test_acc2)/200
                prev_rt=rate_schedule[epoch]
                if epoch==args.n_epoch-1:
                    train_batch[-1][-1]=1
    '''
    curStateBatch = np.asarray([[0.09995994,1],[0.70883413,0.5],[0.72856571,0.5],[0.67888622,0.5]])
    actionBatch = np.asarray([[10,0,0,0]]).T
    nextStateBatch = np.asarray([[0.70883413,0.5],[0.72856571,0.5],[0.67888622,0.5],[0.71804888,0.5]])
    rewardBatch = np.asarray([0.6088742,0.01973151,-0.04967949,0.03916266])
    terminalBatch = np.asarray([0,0,0,1])
    actor=Actor().cuda()
    critic=Critic().cuda()
    actorOptim = torch.optim.Adam(actor.parameters(), lr=args.actor_lr)
    criticOptim = torch.optim.Adam(critic.parameters(), lr=args.critic_lr)

    for jjj in range(args.train_epoch):
        print('RL, epoch:',jjj)
        print(curStateBatch, actionBatch, nextStateBatch, rewardBatch, terminalBatch)

        curStateBatchT=torch.FloatTensor(curStateBatch)
        curStateBatchT=Variable(curStateBatchT).cuda()
        actionBatchT=torch.FloatTensor(actionBatch)
        actionBatchT=Variable(actionBatchT).cuda()
        print(curStateBatchT, actionBatchT)
        
        qPredBatch=critic(curStateBatchT,actionBatchT)
        qTargetBatch=getQTarget(actor, critic, nextStateBatch, rewardBatch, terminalBatch, 0.5)
        print('Predict reward: {}'.format(qPredBatch))
        print('Actual reward: {}'.format(qTargetBatch))

        critic.train()
        criticOptim.zero_grad()
        criticLoss=nn.L1Loss()(qPredBatch, qTargetBatch)
        print('Critic Loss: {}'.format(criticLoss))
        criticLoss.backward()
        criticOptim.step()
        critic.eval()

        actor.train()
        actorOptim.zero_grad()
        actorLoss=-torch.mean(critic(curStateBatchT, actor(curStateBatchT)))
        print('Actor Loss: {}'.format(actorLoss))
        actorLoss.backward()
        actorOptim.step()
        actor.eval()

        mean_pure_ratio1=0
        mean_pure_ratio2=0
        rate_schedule = np.zeros(args.n_epoch)
    
        print('building model...')
        cnn1 = CNN(input_channel=input_channel, n_outputs=num_classes)
        cnn1.cuda()
        print(cnn1.parameters)
        optimizer1 = torch.optim.SGD(cnn1.parameters(), lr=learning_rate)
    
        cnn2 = CNN(input_channel=input_channel, n_outputs=num_classes)
        cnn2.cuda()
        print(cnn2.parameters)
        optimizer2 = torch.optim.SGD(cnn2.parameters(), lr=learning_rate)
    
        epoch=0
        train_acc1=0
        train_acc2=0
        # evaluate models with random weights
        test_acc1, test_acc2=evaluate(test_loader, cnn1, cnn2)
        print('Epoch [%d/%d] Test Accuracy on the %s test images: Model1 %.4f %% Model2 %.4f %% Pure Ratio1 %.4f %% Pure Ratio2 %.4f %%' % (epoch+1, args.n_epoch, len(test_dataset), test_acc1, test_acc2, mean_pure_ratio1, mean_pure_ratio2))
        prev_acc=(test_acc1+test_acc2)/200
        prev_rt=1
        curState = Variable(torch.FloatTensor([prev_acc,prev_rt]), volatile=True).cuda()
        action = getMaxAction(actor, curState)
        curState.volatile = False
        print(curState,action,np.tan(action*np.pi/2))
        rate_schedule[:int(args.n_epoch*split_points[0])] = np.tan(action*np.pi/2)/args.n_epoch*np.arange(int(args.n_epoch*split_points[0]))
        # save results
        with open(txtfile, "a") as myfile:
            myfile.write(str(int(epoch)) + ' '  + str(train_acc1) +' '  + str(train_acc2) +' '  + str(test_acc1) + " " + str(test_acc2) + ' '  + str(mean_pure_ratio1) + ' '  + str(mean_pure_ratio2) + ' ' + str(rate_schedule[epoch]) + "\n")
        # training
        for epoch in range(1, args.n_epoch):
            # train models
            cnn1.train()
            adjust_learning_rate(optimizer1, epoch)
            cnn2.train()
            adjust_learning_rate(optimizer2, epoch)
            train_acc1, train_acc2, pure_ratio_1_list, pure_ratio_2_list=train(train_loader, epoch, cnn1, optimizer1, cnn2, optimizer2, rate_schedule)
            # evaluate models
            test_acc1, test_acc2=evaluate(test_loader, cnn1, cnn2)
            # save results
            mean_pure_ratio1 = sum(pure_ratio_1_list)/len(pure_ratio_1_list)
            mean_pure_ratio2 = sum(pure_ratio_2_list)/len(pure_ratio_2_list)
            print('Epoch [%d/%d] Test Accuracy on the %s test images: Model1 %.4f %% Model2 %.4f %%, Pure Ratio 1 %.4f %%, Pure Ratio 2 %.4f %%' % (epoch+1, args.n_epoch, len(test_dataset), test_acc1, test_acc2, mean_pure_ratio1, mean_pure_ratio2))
            with open(txtfile, "a") as myfile:
                myfile.write(str(int(epoch)) + ': '  + str(train_acc1) +' '  + str(train_acc2) +' '  + str(test_acc1) + " " + str(test_acc2) + ' '  + str(mean_pure_ratio1) + ' '  + str(mean_pure_ratio2) + ' ' + str(rate_schedule[epoch]) + "\n")
            for iii in range(4):
                if epoch == int(args.n_epoch*split_points[iii])-1:
                    curStateBatch[iii][0]=prev_acc
                    curStateBatch[iii][1]=prev_rt
                    actionBatch[iii][0]=action
                    nextStateBatch[iii][0]=(test_acc1+test_acc2)/200
                    nextStateBatch[iii][1]=rate_schedule[epoch]
                    prev_acc=nextStateBatch[iii][0]
                    prev_rt=nextStateBatch[iii][1]
                    rewardBatch[iii]=nextStateBatch[iii][0]-curStateBatch[iii][0]
                    curState = Variable(torch.FloatTensor([prev_acc, prev_rt]), volatile=True).cuda()
                    action = getMaxAction(actor, curState)
                    print(curState,action,np.tan(action*np.pi/2))
                    curState.volatile = False
                    if iii<3:
                        rate_schedule[int(args.n_epoch*split_points[iii]):int(args.n_epoch*split_points[iii+1])] = rate_schedule[epoch]+np.tan(action*np.pi/2)/args.n_epoch*np.arange(int(args.n_epoch*(split_points[iii+1]-split_points[iii])))
                        terminalBatch[iii]=0
                    else:
                        terminalBatch[iii]=1
        print(curStateBatch, actionBatch, nextStateBatch, rewardBatch, terminalBatch)
        
if __name__=='__main__':
    main()
