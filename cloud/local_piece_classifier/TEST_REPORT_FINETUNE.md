# 棋子分类模型微调测试报告

## 测试范围

- 测试清单：`training_dataset/piece_character_dataset/paddleclas/val_list.txt`
- 测试数量：112 张棋子图像
- 来源：8 张互不重叠的原始棋盘图像
- 类别数：14
- 划分方式：按原始棋盘图像分组划分，避免同一原图裁剪出的棋子同时进入训练集和验证集
- 推理设备：CPU

## 对比结果

| 指标 | 微调前 | 微调后 | 变化 |
| --- | ---: | ---: | ---: |
| Top-1 准确率 | 96.43%（108/112） | 100.00%（112/112） | +3.57 个百分点 |
| Top-3 准确率 | 99.11% | 100.00% | +0.89 个百分点 |
| Macro F1 | 95.85% | 100.00% | +4.15 个百分点 |
| 平均 Top-1 置信度 | 77.14% | 89.85% | +12.71 个百分点 |
| 错误数量 | 4 | 0 | -4 |

微调后的模型在该验证集中，14 个类别的 Precision、Recall 和 F1 均为 100%。

## 微调前的 4 个错误

| 实际类别 | 预测类别 | 图像 |
| --- | --- | --- |
| red_shuai | red_shi | `red_shuai/chess-441BF68B2D54-B0CCB19C-000023__c002.jpg` |
| black_xiang | black_ma | `black_xiang/chess-441BF68B2D54-B0CCB19C-000023__c028.jpg` |
| black_che | black_ma | `black_che/chess-441BF68B2D54-B0CCB19C-000010__c026.jpg` |
| red_pao | red_bing | `red_pao/chess-441BF68B2D54-B0CCB19C-000010__c009.jpg` |

微调后的模型已正确识别以上 4 张图像。

## 结论与限制

本次测试表明微调模型在当前数据分布上明显优于旧模型，可以作为当前部署候选模型。

当前结果属于留出验证集评估，不是完全独立的外部测试。部分数据标签来自自动分类或模型辅助分类，因此 100% 不能直接等同于真实场景的最终准确率。建议从不同光照、拍摄角度、距离和棋盘状态下另采集并人工核验一批图像，作为一次性独立测试集后再确定部署指标。

## 产物

- 微调前详细结果：`output/comparison_before/results.json`
- 微调后详细结果：`output/comparison_after/results.json`
- 微调前混淆矩阵：`output/comparison_before/confusion_matrix.csv`
- 微调后混淆矩阵：`output/comparison_after/confusion_matrix.csv`
- 微调后最佳权重：`output/xiangqi_pplcnet_x1_0_finetune/best_model.pdparams`
- 微调后推理模型：`output/xiangqi_pplcnet_x1_0_finetune/inference`
