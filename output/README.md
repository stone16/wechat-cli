# output/

本目录用于存放 **本地生成的最终产物**：

- `wechat-cli export` 导出的 markdown / txt 聊天记录
- `scripts/transcribe_export.py` 转录后的 markdown
- 未来可能加入的报告 / 分析中间件输出

## 为什么放在这里

把生成物统一收在项目内的一个目录，便于：

1. **和源码一起追踪位置**（不用满家目录找"我上周导的那个群"）
2. **隔离个人数据**：整个 `output/` 已被 `.gitignore` 挡住，不会误提交聊天记录
3. **脚本默认落地**：`scripts/transcribe_export.py --output` 不指定时会自动写到这里

## Git 行为

- `output/` 目录本身（靠这个 README）会被追踪
- `output/*.md` / `output/*.txt` / 其他所有内容都被忽略

如果你想追踪某个特定报告（例如对外分享的模板），加 `.gitignore` 的反向规则即可。
