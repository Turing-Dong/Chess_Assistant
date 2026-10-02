# auto_labeled 全量测试报告（2026-10-01）

## 测试范围

- 测试目录：`training_dataset/piece_character_dataset/images/auto_labeled`
- 测试图片：62 张（运行测试时该目录的实际图片数）
- 来源帧：12 个
- 类别：14 类均有样本
- 排除图片：0 张
- 与训练/验证数据来源帧重叠的图片：按要求保留
- 标签来源：自动标注，未经本次人工复核

## 指标

- Top-1 accuracy：`95.1613%`（59/62）
- Top-3 accuracy：`100%`（62/62）
- Macro F1：`95.3209%`
- Top-1 错误：3 张

| 类别 | 正确/样本 | 类别准确率 |
|---|---:|---:|
| red_shuai | 3/4 | 75.00% |
| black_jiang | 3/3 | 100.00% |
| red_shi | 4/4 | 100.00% |
| black_shi | 3/3 | 100.00% |
| red_xiang | 3/3 | 100.00% |
| black_xiang | 3/3 | 100.00% |
| red_ma | 4/4 | 100.00% |
| black_ma | 1/1 | 100.00% |
| red_che | 1/1 | 100.00% |
| black_che | 4/5 | 80.00% |
| red_pao | 4/4 | 100.00% |
| black_pao | 4/4 | 100.00% |
| red_bing | 10/10 | 100.00% |
| black_zu | 12/13 | 92.31% |

## Top-1 错误

1. `black_che/chess-441BF68B2D54-DE6F6994-000003__c003.jpg`：预测为 `black_shi`，置信度 `0.564053`。
2. `black_zu/chess-441BF68B2D54-DE6F6994-000004__c011.jpg`：预测为 `black_shi`，置信度 `0.404940`。
3. `red_shuai/chess-441BF68B2D54-DE6F6994-000003__c024.jpg`：预测为 `red_pao`，置信度 `0.300225`。

完整结果位于 `output/test_evaluation_all_auto_labeled/test_results.json`，混淆矩阵位于同目录的
`confusion_matrix.csv`。

