# Deployment Guide

This guide covers deploying Nexus in different environments.

## Quick Start (Development)

### Prerequisites

- Docker 20.10+
- Docker Compose 2.0+
- (Optional) NVIDIA Docker runtime for GPU support

### Basic Deployment

1. **Clone the repository**

```bash
git clone https://github.com/gevanoff/nexus.git
cd nexus
```

2. **Configure environment**

```bash
cp .env.example .env
# Edit .env and set GATEWAY_BEARER_TOKEN to a secure value
nano .env
```

3. **Start services**

```bash
# Start all services
docker-compose up -d

# Or start specific profile
docker-compose --profile full up -d
```

4. **Verify deployment**

```bash
# Check services are running
docker-compose ps

# Test gateway health
curl http://localhost:8800/health

# Test with authentication
curl -H "Authorization: Bearer YOUR_TOKEN" \
  http://localhost:8800/v1/models
```

## Service Profiles

Nexus uses Docker Compose profiles to control which services run:

### Default Profile
Only gateway and Ollama (minimal deployment):
```bash
docker-compose up -d
```

### Full Profile
All services (gateway, Ollama, images, TTS):
```bash
docker-compose --profile full up -d
```

### Specific Services
```bash
# Images only
docker-compose --profile images up -d

# Audio only
docker-compose --profile audio up -d

# Multiple profiles
docker-compose --profile images --profile audio up -d
```

## Production Deployment

### 1. Security Configuration

**Set strong authentication token:**
```bash
# Generate random token
openssl rand -hex 32

# Set in .env
GATEWAY_BEARER_TOKEN=<your-strong-token>
```

**Configure TLS/HTTPS:**

Option A: Use reverse proxy (recommended)
```bash
# nginx, Caddy, or Traefik in front of gateway
# See nginx example below
```

Option B: Gateway native TLS (simple deployments)
```yaml
gateway:
  environment:
    - GATEWAY_TLS_CERT_PATH=/certs/cert.pem
    - GATEWAY_TLS_KEY_PATH=/certs/key.pem
  volumes:
    - ./certs:/certs:ro
```

### 2. Resource Limits

Add resource limits to docker-compose.yml:

```yaml
services:
  gateway:
    deploy:
      resources:
        limits:
          cpus: '2'
          memory: 2G
        reservations:
          cpus: '1'
          memory: 1G
  
  ollama:
    deploy:
      resources:
        limits:
          cpus: '8'
          memory: 16G
        reservations:
          cpus: '4'
          memory: 8G
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
```

### 3. Persistent Storage

Use host directories for better control:

```yaml
volumes:
  gateway_data:
    driver: local
    driver_opts:
      type: none
      o: bind
      device: /opt/nexus/data/gateway
  
  ollama_data:
    driver: local
    driver_opts:
      type: none
      o: bind
      device: /opt/nexus/data/ollama
```

Create directories:
```bash
sudo mkdir -p /opt/nexus/data/{gateway,ollama,images,tts}
sudo chown -R 1000:1000 /opt/nexus/data
```

### 4. Logging

Configure logging in docker-compose.yml:

```yaml
services:
  gateway:
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"
```

Or use external logging (Loki, ELK, etc.):

```yaml
services:
  gateway:
    logging:
      driver: "loki"
      options:
        loki-url: "http://loki:3100/loki/api/v1/push"
```

### 5. Reverse Proxy (nginx)

Create `nginx.conf`:

```nginx
upstream nexus_gateway {
    server localhost:8800;
}

server {
    listen 443 ssl http2;
    server_name api.yourdomain.com;
    
    ssl_certificate /etc/letsencrypt/live/api.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/api.yourdomain.com/privkey.pem;
    
    # Security headers
    add_header Strict-Transport-Security "max-age=31536000" always;
    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header X-Content-Type-Options "nosniff" always;
    
    # Proxy settings
    location / {
        proxy_pass http://nexus_gateway;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        
        # Timeouts for streaming
        proxy_connect_timeout 300s;
        proxy_send_timeout 300s;
        proxy_read_timeout 300s;
    }
}

# Redirect HTTP to HTTPS
server {
    listen 80;
    server_name api.yourdomain.com;
    return 301 https://$server_name$request_uri;
}
```

### 6. Firewall Configuration

```bash
# Allow only necessary ports
sudo ufw allow 443/tcp  # HTTPS
sudo ufw allow 80/tcp   # HTTP (for Let's Encrypt)

# Deny direct access to service ports
sudo ufw deny 8800/tcp
sudo ufw deny 11434/tcp

sudo ufw enable
```

## High Availability

### Load Balancing

Deploy multiple gateway instances:

```yaml
gateway:
  deploy:
    replicas: 3
```

Use nginx for load balancing:

```nginx
upstream nexus_gateway {
    least_conn;
    server gateway1:8800;
    server gateway2:8800;
    server gateway3:8800;
}
```

### Health Checks

Configure health checks for automatic failover:

```yaml
gateway:
  healthcheck:
    test: ["CMD", "curl", "-f", "http://localhost:8800/health"]
    interval: 30s
    timeout: 10s
    retries: 3
    start_period: 40s
```

### Database Backups

Automated backups for persistent data:

```bash
#!/bin/bash
# backup.sh

BACKUP_DIR="/backups/nexus"
DATE=$(date +%Y%m%d_%H%M%S)

# Backup gateway data
docker run --rm \
  -v nexus_gateway_data:/data \
  -v $BACKUP_DIR:/backup \
  alpine tar czf /backup/gateway-$DATE.tar.gz -C /data .

# Backup ollama models
docker run --rm \
  -v nexus_ollama_data:/data \
  -v $BACKUP_DIR:/backup \
  alpine tar czf /backup/ollama-$DATE.tar.gz -C /data .

# Rotate old backups (keep last 7 days)
find $BACKUP_DIR -name "*.tar.gz" -mtime +7 -delete
```

Schedule with cron:
```bash
0 2 * * * /opt/nexus/scripts/backup.sh
```

## Monitoring

### Prometheus

Create `prometheus.yml`:

```yaml
global:
  scrape_interval: 15s

scrape_configs:
  - job_name: 'nexus-gateway'
    static_configs:
      - targets: ['gateway:8801']
```

Add to docker-compose.yml:

```yaml
prometheus:
  image: prom/prometheus:latest
  volumes:
    - ./prometheus.yml:/etc/prometheus/prometheus.yml
    - prometheus_data:/prometheus
  ports:
    - "9090:9090"
  networks:
    - nexus
```

### Grafana

Add to docker-compose.yml:

```yaml
grafana:
  image: grafana/grafana:latest
  ports:
    - "3000:3000"
  environment:
    - GF_SECURITY_ADMIN_PASSWORD=admin
  volumes:
    - grafana_data:/var/lib/grafana
  networks:
    - nexus
```

### Health Monitoring Script

```bash
#!/bin/bash
# health-check.sh

SERVICES=(
  "http://gateway:8800/health"
  "http://ollama:11434/api/tags"
)

for SERVICE in "${SERVICES[@]}"; do
  if curl -sf "$SERVICE" > /dev/null; then
    echo "✓ $SERVICE is healthy"
  else
    echo "✗ $SERVICE is unhealthy"
    # Send alert (email, Slack, PagerDuty, etc.)
  fi
done
```

## Scaling

### Horizontal Scaling

Scale specific services:

```bash
# Scale Ollama to 3 instances
docker-compose up -d --scale ollama=3

# Gateway load balances automatically
```

### Vertical Scaling

Increase resources per service:

```yaml
services:
  ollama:
    deploy:
      resources:
        limits:
          cpus: '16'
          memory: 32G
```

## Updates and Maintenance

### Update Services

```bash
# Pull latest images
docker-compose pull

# Restart services with new images
docker-compose up -d

# Remove old images
docker image prune -f
```

### Rolling Updates

Update services one at a time:

```bash
# Update gateway
docker-compose up -d --no-deps --force-recreate gateway

# Update Ollama
docker-compose up -d --no-deps --force-recreate ollama
```

### Downtime-Free Updates

1. Add new service version
2. Wait for health check
3. Switch traffic
4. Remove old version

## Troubleshooting

### Services Won't Start

```bash
# Check logs
docker-compose logs

# Check specific service
docker-compose logs gateway

# Check resources
docker stats

# Check disk space
df -h
docker system df
```

### Performance Issues

```bash
# Check resource usage
docker stats

# Check GPU usage
docker-compose exec ollama nvidia-smi

# Check network
docker network inspect nexus_nexus

# Check logs for errors
docker-compose logs | grep -i error
```

### Network Issues

```bash
# Test connectivity between services
docker-compose exec gateway curl http://ollama:11434/api/tags

# Check network
docker network ls
docker network inspect nexus_nexus

# Recreate network
docker-compose down
docker-compose up -d
```

## Environment-Specific Guides

- [Kubernetes Deployment](kubernetes.md)
- [AWS ECS Deployment](aws-ecs.md)
- [Google Cloud Run](gcloud-run.md)
- [Azure Container Instances](azure-aci.md)

## Security Checklist

- [ ] Strong authentication tokens set
- [ ] TLS/HTTPS configured
- [ ] Firewall rules in place
- [ ] Resource limits configured
- [ ] Regular backups scheduled
- [ ] Monitoring and alerting setup
- [ ] Logs being collected
- [ ] Security updates automated
- [ ] Access logs reviewed
- [ ] Secrets stored securely (not in git)

## Best Practices

1. **Use secrets management**: Don't commit tokens to git
2. **Enable monitoring**: Set up Prometheus + Grafana
3. **Configure alerts**: Get notified of failures
4. **Regular backups**: Automate backup process
5. **Update regularly**: Keep services up to date
6. **Review logs**: Check logs for errors regularly
7. **Test disaster recovery**: Practice restoring from backups
8. **Document changes**: Keep deployment docs updated
9. **Use version control**: Track infrastructure changes
10. **Separate environments**: Dev, staging, production

## Support

For deployment issues:
- Check logs: `docker-compose logs`
- Review troubleshooting section
- Open GitHub issue
- Join community discussions
