# Deploying PBC Email Agent to Railway

## Quick Start

1. **Create a new Railway project** at [railway.app](https://railway.app)

2. **Add a PostgreSQL database**:
   - In your project, click "New" → "Database" → "PostgreSQL"
   - Railway auto-injects `DATABASE_URL` into your app

3. **Connect your GitHub repository** or use the Railway CLI:
   ```bash
   railway login
   railway init
   railway up
   ```

4. **Set environment variables** in Railway dashboard:
   - `OPENROUTER_API_KEY` (required) - Your OpenRouter API key (get one at openrouter.ai)

5. **Deploy** - Railway will build using the Dockerfile automatically

## Configuration

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `OPENROUTER_API_KEY` | Yes | OpenRouter API key (provides live pricing) |
| `DATABASE_URL` | Auto | Auto-injected by Railway when you add PostgreSQL |
| `PORT` | No | Auto-set by Railway (default: 8501) |

### Why OpenRouter?

OpenRouter returns **live cost** in API responses. No hardcoded prices — the meter always reflects actual spend.

### Database

The app automatically detects `DATABASE_URL`:
- **If set** → Uses PostgreSQL (production)
- **If not set** → Falls back to SQLite at `data/pbc.db` (local dev)

**Railway PostgreSQL** is the recommended setup:
- Automatic backups
- Persistent data across deploys
- Supports concurrent connections (UI + agent runner)
- No SQLite file locking issues

## Files

| File | Purpose |
|------|---------|
| `Dockerfile` | Container build instructions |
| `railway.toml` | Railway-specific configuration |
| `.env.example` | Environment variable template |

## Monitoring

- Health endpoint: `/_stcore/health`
- Logs: Railway dashboard → Deployments → Logs

## Cost Considerations

- **Railway**: Free tier includes 500 hours/month, then $5/month Hobby plan
- **Railway PostgreSQL**: Free tier includes 1GB storage
- **OpenRouter**: Pay-as-you-go, budget limits enforced in the UI sidebar

## Local Development

```bash
# Set up environment
export OPENROUTER_API_KEY=sk-or-...
export DATABASE_URL=postgresql://user:pass@localhost:5432/pbc  # optional

# Run
streamlit run ui.py
```

## Troubleshooting

### "OPENROUTER_API_KEY not set"
Add the environment variable in Railway dashboard → Variables

### "DATABASE_URL not set" warning
Add a PostgreSQL database to your Railway project (New → Database → PostgreSQL)

### Health check failing
Check logs for Streamlit startup errors. Common issues:
- Missing environment variables
- Database connection failures
