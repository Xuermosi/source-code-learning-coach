# 源码学习教练（Source Code Learning Coach）

简体中文 | [English](README.md)

一个可复用的 Codex Skill：带你从真实源码学习陌生仓库，同时保留可恢复、可追溯的学习过程。

它适合多天源码学习：先给出学习路线、获得确认后再创建学习笔记；每次只讲解一小段真实调用链；并把进度、源码导读、问答、当日总结和可选的流程图产物保存在仓库旁的学习工作区中。

## 能做什么

- 以只读方式扫描仓库，按依赖关系提出按天划分的学习路线与检查点。
- 在创建持久化学习工作区前，先要求你确认学习路线。
- 使用权威 JSON 状态保存可恢复的学习进度，并生成 Markdown 视图。
- 将源码专属笔记和可视化产物归档到对应源码文件下。
- 保护学习工作区路径、符号链接、事务、并发编辑和产物归属关系。
- 每完成一段流程后，先征得明确同意，才生成流程图。
- 默认只在本地操作；要发布学习产物时，必须另建已脱敏副本并完成扫描。

## 本地安装

将此目录复制或克隆到 Codex 的用户级 Skill 目录：

```text
~/.codex/skills/source-code-learning-coach/
```

如果 Skill 没有立即出现，重启 Codex；随后可这样调用：

```text
$source-code-learning-coach
```

如果只想对某个仓库生效，请将本 Skill 放在该仓库中、且位于你的 Codex 配置所支持的仓库级 Skill 目录。

## 使用方式

例如，直接请 Codex 依据真实源码带你学习并保存学习进度：

```text
使用 $source-code-learning-coach 带我从真实源码学习这个仓库，并保存学习进度。
```

这个 Skill 会区分一次性源码解释和持续学习。持续学习只有在你确认路线后，才会在仓库旁创建 `<项目名>-<N>天学习路线` 工作区。

## 目录内容

```text
SKILL.md                         Agent 指令与路由规则
agents/openai.yaml               Codex 界面元数据
assets/templates/                可持续维护的学习笔记模板
references/workspace-contract.md 工作区与状态约定
scripts/manage_learning_workspace.py
                                 工作区管理命令行工具
tests/                           事务、安全性和行为测试
```

## 验证

```bash
python3 -m unittest discover -s tests -p 'test_manage_learning_workspace.py' -v
python3 scripts/manage_learning_workspace.py --help
```

本 Skill 默认只使用本地工作区。将任何学习笔记导出到远程服务前，请先阅读其中的发布规则。
