# polyglot subsets

## val.json — 固定锚点评估集候选池(2026-10-27)

**用途**:GAN 固定根评估(`loop.yaml eval.subset=val` + `eval.anchor_k`)的
候选池;同时作为主链抽题的 held-out 排除集(见 docs/15 §12.4-3)。

**选题规则(确定性、可复现)**:

1. 语言分配按各语言占全集(225 题)的比例,最大余数法取 k=18:
   go 3 / rust 2 / cpp 2 / java 4 / javascript 4 / python 3
   (池占比:go 17.3% / rust 13.3% / cpp 11.6% / java 20.9% / js 21.8% / python 15.1%)。
2. 语言内按 `instance_id` 升序排序后**等距取样**:
   `idx[j] = round(j*(n-1)/(m-1)), j in 0..m-1`(m=该语言选取数,n=该语言池量);
   例:cpp n=26 取 2 → 第 0、25 名;java n=47 取 4 → 第 0、15、31、46 名。
3. 输出按 `instance_id` 全局排序。生成脚本一次性执行,产物即本文件;
   重生成必须复用同一规则并全量 diff 审查。

**边界**:`small.json`/`medium.json` 是 DGM-H 原生 CLI
(`domains/polyglot/harness.py --subset`)的冒烟题单,与评估集无关
(无选题依据记录,勿复用为评估集——docs/15 §12.3)。
