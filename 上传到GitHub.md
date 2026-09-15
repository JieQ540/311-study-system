# 上传到 GitHub · 操作步骤

> 目标目录：`D:\work\kaoyanfuzhu-github\`（已成为清理过敏感信息的副本）
> 你的环境：git 2.55.0 已安装于 `D:\tools\git\cmd\git.exe`

---

## 阶段 0 · 先做两件准备

### 0.1 设置 git 身份（**必做，否则提交会失败**）

你的机器上 `user.name` 和 `user.email` 都还没设置。

```powershell
git config --global user.name "你的名字"
git config --global user.email "你的邮箱@example.com"
```

> 这个邮箱会**公开出现在提交记录里**。不想暴露真实邮箱的话，用 GitHub 提供的
> 隐私邮箱（形如 `12345678+用户名@users.noreply.github.com`，在 GitHub
> Settings → Emails 里能找到）。

**验证**：

```powershell
git config --global user.name
git config --global user.email
```

### 0.2 在 GitHub 上建一个空仓库

1. 打开 <https://github.com/new>
2. 填 Repository name（例如 `311-study-system`）
3. 可见性选 **Public** 或 **Private**（后续都能改）
4. ⚠️ **不要勾选** "Add a README file"、".gitignore"、"license"
   —— 勾了会导致本地推送时冲突，多一步处理
5. 点 **Create repository**

建好后**先别关页面**，你会看到仓库地址（形如
`https://github.com/你的用户名/311-study-system.git`）。

---

## 阶段 1 · 本地提交

```powershell
cd D:\work\kaoyanfuzhu-github

git init
git add .
```

### ★ 关键一步：检查要提交什么

```powershell
git status
```

**逐行看一遍**，确认：

| 应该出现 | 不应该出现 |
| --- | --- |
| `app/`、`tools/`、`docs/`、`source/骨架.md` | `config.json`（含 API key） |
| `README.md`、`config.example.json`、`.gitignore` | `data/*.db`（你的作答数据） |
| `启动系统.bat` | 任何 `.pdf` / `.docx` |

还想更保险，看**具体文件列表**：

```powershell
git ls-files --others --exclude-standard   # 会被加入的文件
```

> 如果发现了不该提交的文件，**先别 commit** —— 把文件名加进 `.gitignore` 再重新 `git add .`。
> 告诉我，我帮你改 `.gitignore`。

### 提交

```powershell
git commit -m "initial commit: 311 备考系统"
```

---

## 阶段 2 · 关联远程仓库并推送

把下面的 `<你的仓库地址>` 换成阶段 0.2 里看到的地址：

```powershell
git branch -M main
git remote add origin <你的仓库地址>
git push -u origin main
```

**示例**（换成你自己的）：

```powershell
git branch -M main
git remote add origin https://github.com/yourname/311-study-system.git
git push -u origin main
```

### 如果提示要密码 / 认证失败

GitHub 从 2021 年起**不再接受账号密码**，要用 **Personal Access Token**：

1. <https://github.com/settings/tokens> → Generate new token (classic)
2. 勾选 **repo** 权限，生成后**立刻复制**（页面关了就看不再）
3. 推送时把 token 当密码粘贴

或者改用 SSH（一次性配置，之后免密）：

```powershell
ssh-keygen -t ed25519 -C "你的邮箱"
# 然后把 ~/.ssh/id_ed25519.pub 的内容贴到 https://github.com/settings/keys
git remote set-url origin git@github.com:yourname/311-study-system.git
```

---

## 阶段 3 · 推上去之后再检查一次

打开你的仓库页面，确认：

- [ ] `README.md` 正常显示
- [ ] **没有** `config.json`
- [ ] **没有** 任何 `.db` 文件
- [ ] **没有** PDF
- [ ] 文件数大致在 60 个左右（我这边统计是 **59 个物理文件**）

---

## 常见问题

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `Please tell me who you are` | 没做阶段 0.1 | 设置 user.name / user.email |
| `remote origin already exists` | 之前加过 | `git remote set-url origin <新地址>` |
| `failed to push some refs` | 远程仓库非空（建时勾了 README） | `git pull --rebase origin main` 后再 push |
| 推送很慢或失败 | 网络问题 | 配置代理，或改用 SSH |
| 误把 key 推上去了 | —— | **立刻去平台吊销该 key**，然后 `git rm --cached config.json` 重新提交 |

> ⚠️ **最后一条最重要**：万一 API key 被推上去，**先吊销 key**，再处理仓库历史。
> 因为 Git 的历史记录会保留，删文件不等于删记录。

---

## 后续更新（改了代码想再推）

```powershell
cd D:\work\kaoyanfuzhu-github
git add .
git commit -m "描述这次改了什么"
git push
```

---

## 需要我代劳哪部分？

**我可以做的**（本地、可逆）：

- `git init` + `git add .` + 生成一份"将要提交的文件清单"给你过目
- 帮你写/改 `.gitignore`
- `git commit`（需要你先设好身份，或告诉我用什么名字和邮箱）

**我不能做的**：

- **推送**（需要你的 GitHub 账号凭据，不该由我持有）
- 替你决定仓库用 Public 还是 Private
