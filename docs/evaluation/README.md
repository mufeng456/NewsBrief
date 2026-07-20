# NewsBrief 竞赛评测规范

## 数据边界

评测集固定为 60 篇新闻：校园、民生、政策、科技、财经、文化各 10 篇。每篇来源限定为政府、公共机构或高校官网，并在公开元数据中记录标题、URL、发布日期、采集日期、内容 SHA-256 与使用说明。

仓库不提交新闻正文快照或详细标注。运行本机评测时，将私有 `articles.json` 与 `annotations.json` 放入 `backend/benchmark_private/`；该目录已被 Git 忽略。

## 私有文件结构

`articles.json` 为数组，每项包含 `id`、`title`、`content`、`category`。`annotations.json` 以新闻 `id` 为键，每项包含 `key_sentence_ids`、`number_values`、`reference_summary`、`facts` 与 `review_note`。

## 标注与盲评

关键句和六要素事实先由一人初标，再由第二人复核；分歧写入 `review_note`。人工盲评抽取 24 篇均衡样本，对 Lead-3、基础 TextRank、V2.0 三种匿名结果按事实忠实度、关键信息覆盖度、阅读清晰度、可信感进行 1–5 分评分。

5–8 名评测者采用平衡随机分配，确保每个“新闻-系统”组合至少获得 3 份评分。报告必须公开均值、中位数、离散度和 95% Bootstrap 区间，不得选择性省略结果。
