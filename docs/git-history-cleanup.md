# Git 历史清理说明

当前提交会停止继续跟踪运行时行情、提示、日志、PID 和 Python 字节码，但普通删除不会抹掉这些文件在既有 Git 提交中的内容。若仓库曾公开，旧提交仍可能包含运行数据、本机路径或已删除模块的字节码。

不要在日常提交中直接重写历史。执行前必须满足以下条件：

1. 已取得仓库所有维护者的明确授权，并通知所有协作者暂停推送。
2. 已创建可验证的远端备份或镜像备份。
3. 已记录当前分支、标签和远端提交 ID，确认可以回滚。
4. 已安装并理解 `git filter-repo`；重写会改变所有受影响提交 ID，现有克隆、分支、标签和开放中的 PR 都需要重新同步。

在得到上述授权后，可在一次性镜像克隆中按实际敏感路径编写 `git filter-repo --path ... --invert-paths` 命令。路径清单至少应审查：

- `data/monitor/quotes.json`
- `data/monitor/quotes.jsonl`
- `data/monitor/alerts.jsonl`
- `data/monitor/history/`
- `**/__pycache__/`
- `**/*.pyc`

不要照抄未经核对的删除命令；先用 `git log --all --name-only` 确认精确目标，再在备份克隆中演练。重写完成后应验证：

```powershell
git fsck --full
git log --all --name-only -- data/monitor/quotes.json data/monitor/quotes.jsonl data/monitor/alerts.jsonl
$sensitivePattern = Read-Host '输入旧路径的正则（仅保存在本次会话变量中）'
$historyMatches = $false
foreach ($revision in (git rev-list --all)) {
    git grep -I -n -E -- $sensitivePattern $revision
    if ($LASTEXITCODE -eq 0) {
        $historyMatches = $true
    } elseif ($LASTEXITCODE -ne 1) {
        throw "历史内容扫描失败: $revision"
    }
}
if ($historyMatches) { throw '可达提交中仍存在敏感内容' }
```

上述循环使用 `git grep` 检查每个可达提交的 blob 内容，而不把真实旧路径写入文档或仓库。扫描没有输出且最终未抛错，表示当前 refs 可达的提交中没有匹配；退出码 1 表示该提交无匹配，其他退出码视为扫描失败。reflog、备份 refs、远端旧 refs 和不可达对象不在这个结论内，仍须按备份与协作计划单独检查，并在确认不再需要后由维护者处理。

确认扫描无残留、测试通过并经维护者复核后，才可以协调强制更新远端。所有旧克隆都应重新克隆，不能继续在旧历史上推送。本项目本次改造**不会自动执行历史重写**。
