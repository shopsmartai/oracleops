# OracleOps on OCI Always Free Tier

Get OracleOps running on Oracle Cloud's permanent free tier in about 45 minutes. Total cost: $0/month.

## What you need

| OCI resource | Free allocation | Why |
|---|---|---|
| Compute (Ampere ARM A1) | 4 OCPUs, 24 GB RAM total (split across up to 4 VMs) | Host the Hermes Agent daemon |
| Autonomous Database (ATP or ADW) | 2 instances × 20 GB | Your test database + agent state store |
| Object Storage | 20 GB | AWR exports, postmortem artifacts |
| Virtual Cloud Network | Free | Networking for the VM |
| Outbound bandwidth | 10 TB/month | Telegram polling + AI provider API calls |

These are *Always Free* (not 30-day trial) as of May 2026. Sign up at [oracle.com/cloud/free](https://www.oracle.com/cloud/free/).

## Step 1: Provision the compute VM

In the OCI console:

1. **Menu → Compute → Instances → Create Instance**
2. Name: `oracleops-host`
3. Image: **Oracle Linux 9** (free, supported, ships with `dnf`)
4. Shape: **Ampere A1.Flex** with 2 OCPUs and 12 GB memory (leaves headroom in your free allocation)
5. Networking: Use the default VCN; assign a **public IPv4** (you'll SSH in)
6. SSH keys: Upload your public key
7. Create

Wait ~2 minutes for `Running`. Note the public IP.

## Step 2: Open the firewall for Telegram

OCI's default ingress rules block outbound webhooks but Telegram uses long-polling, so you only need outbound (which is open by default). Optional: if you want to use Telegram webhook mode later, open ingress on port 8443.

```
VCN → Security Lists → Default → Add Ingress Rule
  Source CIDR:   0.0.0.0/0
  IP Protocol:   TCP
  Destination port: 8443
```

Skip this step if you'll only use long-polling (the default and recommended setup).

## Step 3: Connect and install dependencies

```bash
ssh -i ~/.ssh/your-key opc@<public-ip>

sudo dnf update -y
sudo dnf install -y python3.11 python3.11-pip git tmux

# Required for oracledb thin mode on ARM Oracle Linux
sudo dnf install -y libaio
```

## Step 4: Install Hermes Agent

```bash
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
hermes setup
```

The setup wizard will ask for your model provider. For the free path:

- **OpenRouter** with a free-tier API key — supports many models including Gemma 3, Llama 3, and the Nous Hermes models themselves
- **Nous Portal** if you have access
- **Gemini** with a free Google AI Studio key (fastest)

## Step 5: Get the Autonomous Database connection

In the OCI console:

1. **Menu → Oracle Database → Autonomous Database**
2. Click your existing instance (you mentioned one is already configured)
3. **Database Connection** → **Download Wallet**
4. Set a wallet password and download the zip

On the VM:

```bash
mkdir -p ~/oracle-wallet
cd ~/oracle-wallet
# Upload the wallet zip via scp from your laptop, then:
unzip Wallet_<DB_NAME>.zip
```

You'll see files including `tnsnames.ora`, `sqlnet.ora`, `cwallet.sso`. Inspect `tnsnames.ora` for the connection aliases — typically `<dbname>_high`, `<dbname>_medium`, `<dbname>_low`, `<dbname>_tp`, `<dbname>_tpurgent`. Use `_high` for OracleOps (it gets the most resources).

Configure Hermes:

```bash
hermes config set ORACLE_CONNECTION_STRING "admin/YOUR_ADMIN_PASSWORD@<dbname>_high"
hermes config set ORACLE_WALLET_DIR "/home/opc/oracle-wallet"
hermes config set ORACLE_WALLET_PASSWORD "your_wallet_password"
```

## Step 6: Install the OracleOps skills and plugin

```bash
cd ~
git clone https://github.com/YOUR_HANDLE/oracleops.git
cd oracleops

# Skills go into Hermes' skills directory
mkdir -p ~/.hermes/skills
cp -r skills/* ~/.hermes/skills/

# Plugin goes into Hermes' plugins directory
mkdir -p ~/.hermes/plugins
cp -r plugins/oracle ~/.hermes/plugins/

# Install the plugin's Python dependencies
pip3.11 install --user -r ~/.hermes/plugins/oracle/requirements.txt
```

## Step 7: Verify Oracle connectivity

```bash
python3.11 -c "
from oracle.connection import get_pool
pool = get_pool()
with pool.connection() as conn:
    cur = conn.cursor()
    cur.execute('SELECT user, sysdate FROM dual')
    print(cur.fetchone())
"
```

If you see something like `('ADMIN', datetime.datetime(2026, 5, 18, ...))`, you're good.

## Step 8: Configure Telegram

1. Open Telegram, search for `@BotFather`, run `/newbot`
2. Pick a name (e.g., `OracleOps DBA`) and a username ending in `bot`
3. Copy the bot token

On the VM:

```bash
hermes gateway add telegram --token <YOUR_BOT_TOKEN>
hermes gateway list  # confirm it's registered
```

## Step 9: Start Hermes

Run it in a `tmux` session so it survives logout:

```bash
tmux new -s hermes
hermes serve
```

Detach with `Ctrl+B` then `D`. Reattach with `tmux attach -t hermes`.

## Step 10: Send your first message

In Telegram, find your bot. Send:

```
/start
```

The bot should reply with a Hermes welcome. Then:

```
What's happening with the database right now?
```

The agent should pick up the `awr-summary-now` skill, run the queries against your ADB, and reply with the summary.

If it doesn't trigger the skill, run:

```
list available skills
```

The agent will print what it sees. If OracleOps skills aren't there, check `~/.hermes/skills/` — the SKILL.md files need to be one directory level deep (e.g., `~/.hermes/skills/awr-summary-now/SKILL.md`).

## Troubleshooting

**`ORA-12506: TNS:listener rejected connection`**
Wallet directory wrong. Confirm `tnsnames.ora` is in `ORACLE_WALLET_DIR`.

**`ORA-28759: failure to open file`**
Wallet password wrong, or `cwallet.sso` is missing/corrupted. Re-download.

**`ORA-00942: table or view does not exist`** on AWR queries
Your DB user doesn't have AWR privileges. Run as `SYSDBA`:
```sql
GRANT SELECT ANY DICTIONARY TO admin;
```
On Autonomous Database, `ADMIN` already has this. If you connect as a different user, grant it.

**Bot doesn't respond**
Check `tmux attach -t hermes` for errors. Common causes: invalid Telegram token, missing model provider API key, model provider rate limit.

**`hermes config set` says command not found**
Add `~/.local/bin` to PATH: `echo 'export PATH=$PATH:~/.local/bin' >> ~/.bashrc && source ~/.bashrc`.

## Cost watchdog

OCI's free tier is generous but not unlimited. To stay at $0:

- Don't add a second compute instance (free tier covers up to 4 ARM OCPUs total; one VM with 2 OCPUs leaves headroom).
- Don't enable backup retention longer than 7 days on ADB (free allows up to 60 days, but storage past the 20 GB free allocation is billed).
- Don't enable Performance Hub history retention longer than the default 8 days on ADB.
- Watch the `Cost Analysis` tab in the OCI console weekly during the hackathon.

Set a $1 budget alert (Menu → Billing → Budgets) — if anything triggers a charge, you'll know immediately.

## Next

Once the bot is running, try these (real, useful) skills against your ADB:

- "Show me the top SQL by elapsed time in the last hour"
- "Are any sessions blocking?"
- "Describe the `SALES` table"
- "Explain plan for: `SELECT * FROM SALES WHERE channel_id = 3`"

For each, the agent will pick the right OracleOps skill, run it against your real ADB, and reply with structured output.
