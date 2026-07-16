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
   - `ANTHROPIC_API_KEY` (required) - Your Anthropic API key

5. **Deploy** - Railway will build using the Dockerfile automatically

## Configuration

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `ANTHROPIC_API_KEY` | Yes | Anthropic API key for Claude models |
| `DATABASE_URL` | Auto | Auto-injected by Railway when you add PostgreSQL |
| `PORT` | No | Auto-set by Railway (default: 8501) |

### Database

The app automatically detects `DATABASE_URL`:
- **If set** → Uses PostgreSQL (production)
- **If not set** → Falls back to SQLite at `data/pbc.db` (local dev)

**Railway PostgreSQL** is the recommended setup:
- Automatic backups
- Persistent data across deploys
- Supports concurrent connections (UI + agent runner)
- No SQLite file locking issues

### Lighter Deployment (Recommended)

The default deployment uses `requirements-railway.txt` which omits 
`sentence-transformers` (~2GB+ with PyTorch). The app falls back to 
hashed-ngram matching which works well for PBC item matching.

To use full ML embeddings, edit the Dockerfile to use `requirements.txt` instead.

## Files Created

| File | Purpose |
|------|---------|
| `Dockerfile` | Container build instructions |
| `railway.toml` | Railway-specific configuration |
| `requirements-railway.txt` | Lightweight dependencies + psycopg2 |
| `.railwayignore` | Files to exclude from deployment |
| `.env.example` | Environment variable template |

## Monitoring

- Health endpoint: `/_stcore/health`
- Logs: Railway dashboard → Deployments → Logs

## Cost Considerations

- **Railway**: Free tier includes 500 hours/month, then $5/month Hobby plan
- **Railway PostgreSQL**: Free tier includes 1GB storage
- **Anthropic API**: Set budget limits in the UI sidebar to avoid runaway costs

## Local Development with PostgreSQL

To test PostgreSQL locally before deploying:

```bash
# Start a local PostgreSQL (using Docker)
docker run -d --name pbc-postgres \
  -e POSTGRES_PASSWORD=localpass \
  -e POSTGRES_DB=pbc \
  -p 5432:5432 \
  postgres:15

# Set DATABASE_URL and run
export DATABASE_URL="postgresql://postgres:localpass@localhost:5432/pbc"
streamlit run ui.py
```

## Alternative: Docker Compose (Self-hosted)

```yaml
# docker-compose.yml
version: '3.8'
services:
  app:
    build: .
    ports:
      - "8501:8501"
    environment:
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
      - DATABASE_URL=postgresql://postgres:password@db:5432/pbc
    depends_on:
      - db
  db:
    image: postgres:15
    environment:
      - POSTGRES_PASSWORD=password
      - POSTGRES_DB=pbc
    volumes:
      - pgdata:/var/lib/postgresql/data

volumes:
  pgdata:
```

Run with:
```bash
ANTHROPIC_API_KEY=sk-ant-... docker-compose up
```

## Troubleshooting

### "ANTHROPIC_API_KEY not set"
Add the environment variable in Railway dashboard → Variables

### "DATABASE_URL not set" warning
Add a PostgreSQL database to your Railway project (New → Database → PostgreSQL)

### Connection errors to PostgreSQL
- Check that the PostgreSQL service is running in Railway
- Verify `DATABASE_URL` is properly injected (check Variables tab)

### Build timeout
Railway allows 20min builds by default. The slim image should build in <5min.

### Health check failing
Check logs for Streamlit startup errors. Common issues:
- Missing environment variables
- Database connection failures
- Port conflicts (Railway auto-assigns PORT)

## Migration from SQLite

If you have existing data in SQLite that you want to migrate to PostgreSQL:

1. Export data from SQLite (use a tool like `sqlite3` or DBeaver)
2. Import into PostgreSQL
3. The schema is identical, just different placeholder syntax (`?` vs `%s`)

Note: The app handles this automatically - same Python code works with both databases.
