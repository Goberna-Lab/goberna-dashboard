import os
import sys
from pathlib import Path
import pymysql

# Usar PyMySQL como driver para compatibilidad Serverless
pymysql.install_as_MySQLdb()

BASE_DIR = Path(__file__).resolve().parent.parent

# SEGURIDAD: Leer de entorno
SECRET_KEY = os.getenv('SECRET_KEY', 'django-insecure-dummy-key-for-build')
DEBUG = os.getenv('DEBUG', 'False') == 'True'

ALLOWED_HOSTS = ['*'] # Vercel usa dominios dinámicos, restringir en prod si se desea

INSTALLED_APPS = [
    'django.contrib.admin', # Opcional, quizás para debug
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    
    'core', # Nuestra app
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware', # Servir estáticos
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'dashboard_project.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'core' / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'dashboard_project.wsgi.app' # Variable 'app' para Vercel

# BASE DE DATOS
# Se inyectarán via variables en Vercel
# (Import eliminado por no uso y error en deployment)
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.mysql',
        'NAME': os.getenv('DB_NAME', 'goberna_db'),
        'USER': os.getenv('DB_USER', 'root'),
        'PASSWORD': os.getenv('DB_PASSWORD', ''),
        'HOST': os.getenv('DB_HOST', 'localhost'),
        'PORT': os.getenv('DB_PORT', '3306'),
        'OPTIONS': {
            'init_command': "SET sql_mode='STRICT_TRANS_TABLES'",
             # SSL suele ser necesario para conexiones remotas seguras
             # 'ssl': {'ca': '/path/to/ca-cert.pem'} if os.getenv('DB_SSL') else None
        },
    }
}

# AUTH PASSWORD VALIDATORS (Standard)
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
]

# INTERNATIONALIZATION
LANGUAGE_CODE = 'es-pe'
TIME_ZONE = 'America/Lima'
USE_I18N = True
USE_TZ = True

# STATIC FILES
STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

# MEDIA FILES (Importante: Vercel no guarda archivos, leer desde S3 o URL externa)
MEDIA_URL = os.getenv('MEDIA_URL', '/media/') 
# MEDIA_ROOT no sirve en Vercel para escritura, solo lectura si se despliegan assets

# SESIÓN COMPARTIDA (CLAVE PARA EL LOGIN)
# Ajustar esto al dominio real, ej: ".goberna.pe"
SESSION_COOKIE_DOMAIN = os.getenv('SESSION_COOKIE_DOMAIN', None) 
SESSION_ENGINE = 'django.contrib.sessions.backends.db' # Comparte sesión por DB

# URLs EXTERNAS (Para los links del dashboard)
MAIN_APP_URL = os.getenv('MAIN_APP_URL', 'https://app.goberna.pe')

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
