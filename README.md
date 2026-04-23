# Guía de Despliegue en Vercel - Dashboard Satélite - v0.1.2

Esta carpeta contiene una "Mini App Django" diseñada para ejecutarse en Vercel y conectarse a tu base de datos MySQL existente.

## 1. Preparación del Repositorio
Para desplegar esto, lo más limpio es crear un repositorio nuevo en GitHub solo con el contenido de esta carpeta.

1. Crea un repo en GitHub llamado `goberna-dashboard` (o similar).
2. Sube **solo el contenido** de `dashboard_satellite` a la raíz de ese repo.
   (Es decir, `manage.py`, `vercel.json` y `requirements.txt` deben estar en la raíz del repo).

## 2. Configuración en Vercel
Al importar el proyecto en Vercel:
- **Framework Preset:** Other (o Django si lo detecta).
- **Environment Variables:** Debes agregar TODAS estas variables (Settings > Environment Variables):

| Variable | Valor | Descripción |
|----------|-------|-------------|
| `DB_NAME` | `nombre_tu_bd` | Nombre real de la base de datos MySQL |
| `DB_USER` | `usuario_remoto` | Usuario con permisos de acceso remoto (%) |
| `DB_PASSWORD` | `tu_password` | Contraseña del usuario |
| `DB_HOST` | `1.2.3.4` | IP Pública de tu servidor VPS |
| `DB_PORT` | `3306` | Puerto MySQL (Asegúrate que el firewall del servidor permita conexión desde fuera) |
| `SECRET_KEY` | `genera_una_clave_larga_aleatoria` | Clave de seguridad de Django |
| `DEBUG` | `False` | En producción siempre False |
| `MAIN_APP_URL` | `https://app.goberna.pe` | URL de tu sistema principal (sin slash final) |
| `SESSION_COOKIE_DOMAIN` | `.goberna.pe` | (Opcional) Dominio raíz para compartir sesión |

## 3. Login Compartido (Single Sign-On)
Para que el usuario no tenga que loguearse dos veces:
1. El usuario se loguea en tu app principal.
2. La app principal guarda la cookie en `.goberna.pe` (dominio raíz).
3. Cuando entra al dashboard en Vercel (`dashboard.goberna.pe`), Vercel lee esa cookie y la base de datos, reconociendo al usuario.

**Requisito:**
En el `settings.py` de TU SERVIDOR PRINCIPAL, debes agregar:
```python
SESSION_COOKIE_DOMAIN = '.goberna.pe'  # Ajusta a tu dominio real
```

## 4. Notas Técnicas
- **Archivos Estáticos:** El CSS del sidebar y librerías externas se cargan correctamente. Las imágenes de perfil se cargan desde tu servidor principal.
- **Base de Datos:** La app en Vercel NO realiza migraciones (`managed=False`). Solo lee y escribe datos usando la estructura existente.
- **Rendimiento:** La primera carga puede tardar unos segundos ("Cold Start" de Vercel), luego será rápida.
