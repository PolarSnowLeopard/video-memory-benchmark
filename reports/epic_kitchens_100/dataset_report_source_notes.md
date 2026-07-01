# EPIC-KITCHENS-100 HTML 报告源说明

## 报告结构映射

- 技术摘要：对应报告核心结论。
- 数据范围和组成结构、视频粒度、动作标注、动作和物体分布、筛选建议：对应带图表证据的主要发现。
- 数据范围、资料来源、复现路径：对应数据范围、来源和指标口径。
- 存储和预处理成本、限制和待验证问题：对应方法说明和局限性。
- 建议的下一步：对应后续工作建议与待回答问题。

## 图表地图

- `source_composition`：组成：按来源比较数量、时长、体积占比；形式为 100% 堆叠横条。 输出 `reports/epic_kitchens_100/assets/source_composition.png`。
- `duration_distribution`：时长分布：700 个视频时长直方图。 输出 `reports/epic_kitchens_100/assets/duration_distribution.png`。
- `participant_duration`：参与者排行：按总视频时长排序的前 15 名。 输出 `reports/epic_kitchens_100/assets/participant_duration.png`。
- `split_segments`：动作片段覆盖：训练、验证、测试时间戳数量。 输出 `reports/epic_kitchens_100/assets/split_segments.png`。
- `action_duration`：动作时长分布：训练/验证动作片段，截断到 30 秒。 输出 `reports/epic_kitchens_100/assets/action_duration.png`。
- `top_verbs`：动词长尾：训练/验证中最高频的官方动词类别。 输出 `reports/epic_kitchens_100/assets/top_verbs.png`。
- `top_nouns`：名词长尾：训练/验证中最高频的官方名词类别。 输出 `reports/epic_kitchens_100/assets/top_nouns.png`。
- `noun_categories`：名词上位类别：训练/验证动作标签关联的名词上位类别。 输出 `reports/epic_kitchens_100/assets/noun_categories.png`。
- `duration_density`：筛选关系：视频时长与动作密度散点。 输出 `reports/epic_kitchens_100/assets/duration_density.png`。
- `storage`：存储估算：原始视频和代理视频量级比较。 输出 `reports/epic_kitchens_100/assets/storage.png`。

## 关键口径

- 原始视频总大小来自服务器对 700 个远端 MP4 文件头的 `Content-Length` 求和。
- 540p 全量代理体积按 P04 三个样本的 540p 转码大小外推。
- 测试集只有公开时间戳，不含公开动词/名词标签，因此动作/物体分布只使用训练和验证集。
- 旧版 EPIC-KITCHENS-55 视频被纳入 EPIC-KITCHENS-100，但报告统一使用 EPIC-KITCHENS-100 标注体系。
