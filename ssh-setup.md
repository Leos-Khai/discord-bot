Absolutely! Here's a clear and concise **ToDo-style guide** to configure Git to use your SSH key on **both WSL (Linux)** and **Windows**, perfect as a quick reference.

---

## ✅ Git SSH Setup: ToDo Guide for Windows and WSL

---

### 📌 STEP 1: Generate SSH Key (If Not Already Done)

✅ On WSL (Linux):

```bash
ssh-keygen -t ed25519 -C "your_email@example.com"
```

> Press Enter to accept default path (`~/.ssh/id_ed25519`).  
> Set a passphrase if desired.

✅ On Windows (PowerShell or Git Bash):

```bash
ssh-keygen -t ed25519 -C "your_email@example.com"
```

> Save to: `C:\Users\YourUsername\.ssh\id_ed25519`

---

### 📌 STEP 2: Add SSH Key to GitHub/GitLab/Bitbucket

✅ On WSL:

```bash
cat ~/.ssh/id_ed25519.pub
```

✅ On Windows:

```powershell
type $env:USERPROFILE\.ssh\id_ed25519.pub
```

→ Copy the entire output  
→ Go to GitHub → Settings → SSH and GPG Keys → New SSH Key  
→ Paste it, give a name, Save.

---

### 📌 STEP 3: Configure Git to Use SSH Key

#### ✅ On WSL:

1. **Ensure SSH agent is running:**

```bash
eval "$(ssh-agent -s)"
```

2. **Add key to agent:**

```bash
ssh-add ~/.ssh/id_ed25519
```

3. **Test connection:**

```bash
ssh -T git@github.com
```

> Should say: “You've successfully authenticated…”

---

#### ✅ On Windows:

1. **Start Git Bash or PowerShell**

2. **Ensure SSH agent is running (Git Bash):**

```bash
eval "$(ssh-agent -s)"
```

3. **Add key to agent:**

```bash
ssh-add ~/.ssh/id_ed25519
```

> Or in PowerShell:

```powershell
Start-Service ssh-agent
ssh-add $env:USERPROFILE\.ssh\id_ed25519
```

4. **Test connection:**

```bash
ssh -T git@github.com
```

---

### 📌 STEP 4: Use SSH Instead of HTTPS in Git Remotes

✅ Change remote URL:

```bash
git remote set-url origin git@github.com:username/repo.git
```

✅ Verify:

```bash
git remote -v
```

---

### 📌 STEP 5: (Optional) Configure Git Identity

✅ On both systems:

```bash
git config --global user.name "Your Name"
git config --global user.email "your_email@example.com"
```

---

### 📌 STEP 6: (Optional) Use the Same Key Across WSL and Windows

✅ From WSL, copy SSH key to Windows:

```bash
cp ~/.ssh/id_ed25519 /mnt/c/Users/YourUsername/.ssh/
cp ~/.ssh/id_ed25519.pub /mnt/c/Users/YourUsername/.ssh/
```

✅ Or access WSL key from Windows:

```powershell
ssh-add "\\wsl$\Ubuntu\home\leos_khai\.ssh\id_ed25519"
```

✅ Or use symlink (advanced):
You can symlink `~/.ssh` between WSL and Windows to share keys.

---

### ✅ Summary

| Task             | WSL Command                                   | Windows Command (PowerShell or Git Bash)    |
| ---------------- | --------------------------------------------- | ------------------------------------------- |
| Generate SSH Key | `ssh-keygen -t ed25519`                       | `ssh-keygen -t ed25519`                     |
| Show Public Key  | `cat ~/.ssh/id_ed25519.pub`                   | `type $env:USERPROFILE\.ssh\id_ed25519.pub` |
| Start SSH Agent  | `eval "$(ssh-agent -s)"`                      | `Start-Service ssh-agent`                   |
| Add Key to Agent | `ssh-add ~/.ssh/id_ed25519`                   | `ssh-add $env:USERPROFILE\.ssh\id_ed25519`  |
| Test GitHub SSH  | `ssh -T git@github.com`                       | `ssh -T git@github.com`                     |
| Set Remote URL   | `git remote set-url origin git@github.com...` | Same                                        |

---

Would you like this turned into a downloadable `.md` file for your personal notes?