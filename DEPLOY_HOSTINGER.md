# Deploying DPR_Project on Hostinger VPS

This is a Django project. Hostinger's Web/Cloud hosting plans do not run Django apps directly; use a Hostinger VPS plan for this project.

## 1. Prepare VPS

Choose a Linux VPS. Hostinger offers a Django/OpenLiteSpeed VPS template, or you can use Ubuntu and install the stack manually.

Manual Ubuntu setup:

```bash
sudo apt update
sudo apt install python3 python3-venv python3-dev build-essential default-libmysqlclient-dev pkg-config mysql-server nginx
```

## 2. Upload Project

Upload the `DPR_Project` folder to the VPS, for example:

```bash
/var/www/dpr/DPR_Project
```

The folder containing `manage.py` should be the app root.

## 3. Create Virtual Environment

```bash
cd /var/www/dpr/DPR_Project
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirement.txt
```

## 4. Configure Environment

Create a `.env` file or set these variables in your process manager:

```bash
DJANGO_DEBUG=False
DJANGO_SECRET_KEY=replace-with-a-long-random-secret
DJANGO_ALLOWED_HOSTS=yourdomain.com,www.yourdomain.com
DJANGO_CSRF_TRUSTED_ORIGINS=https://yourdomain.com,https://www.yourdomain.com

MYSQL_DATABASE=dpr_db
MYSQL_USER=dpr_user
MYSQL_PASSWORD=replace-with-database-password
MYSQL_HOST=localhost
MYSQL_PORT=3306
```

If using systemd, place these values in the service file with `Environment=...` lines or an `EnvironmentFile`.

## 5. Create Database

```bash
sudo mysql
CREATE DATABASE dpr_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'dpr_user'@'localhost' IDENTIFIED BY 'replace-with-database-password';
GRANT ALL PRIVILEGES ON dpr_db.* TO 'dpr_user'@'localhost';
FLUSH PRIVILEGES;
EXIT;
```

## 6. Migrate and Collect Static Files

```bash
source .venv/bin/activate
python manage.py migrate
python manage.py collectstatic
python manage.py createsuperuser
```

## 7. Run with Gunicorn

```bash
pip install gunicorn
gunicorn DPR_Project.wsgi:application --bind 127.0.0.1:8000
```

For a permanent setup, run Gunicorn using `systemd` and proxy traffic to it from Nginx/OpenLiteSpeed.

## 8. Static and Media Paths

Configure the web server to serve:

```text
/static/ -> /var/www/dpr/DPR_Project/staticfiles/
/media/  -> /var/www/dpr/DPR_Project/media/
```

## 9. Final Checks

```bash
python manage.py check --deploy
```

Then point your domain DNS to the VPS IP and enable SSL in Hostinger/hPanel or on the server.
