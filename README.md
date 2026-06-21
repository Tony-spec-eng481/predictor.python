# Road Worx Analytics Backend

A real-time analytics and predictive backend for the Road Worx game, built with Flask, Socket.IO, and Playwright.

## 🚀 Features

- **Real-time Data Collection**: Automated Playwright collector to monitor round results.
- **Provably Fair Verification**: Independent cryptographic verification of crash points using SHA-512.
- **Advanced Predictor**: Statistical and cryptographic models to analyze round patterns.
- **WebSocket Integration**: Live updates pushed to the frontend via Socket.IO.
- **Strategy Simulation**: Backtesting engine for various betting strategies.

## 🛠️ Setup & Installation

### 1. Prerequisites
- Python 3.9+
- Node.js (for Playwright browser installation)

### 2. Environment Setup
```bash
# Create virtual environment
python -m venv venv

# Activate virtual environment
# On Linux/macOS:
source venv/bin/activate
# On Windows:
venv\Scripts\activate

# Install dependencies
pip install -r app/requirements.txt

# Install Playwright browsers
playwright install chromium
```

### 3. Configuration
Copy the `.env.example` to `.env` and fill in your details:
```bash
cp app/.env.example app/.env
```

## 🏃 Running the Application

### Development Mode
```bash
cd app
python app.py
```

### Production Mode
Using the provided start script:
```bash
cd app
./start.sh
```
Or manually using Gunicorn:
```bash
gunicorn wsgi:application --worker-class geventwebsocket.gunicorn.workers.GeventWebSocketWorker --workers 1 --bind 0.0.0.0:5000
```

## 🌐 Hosting Instructions

### Railway / Render / Heroku
This project is pre-configured with a `Procfile` and `wsgi.py`.
1. Push this repository to GitHub.
2. Connect your repository to your hosting provider.
3. Add the environment variables from `.env.example` to your provider's dashboard.
4. Ensure the build command installs requirements and the start command uses the `Procfile`.

### Manual VPS (Nginx + Gunicorn)
1. Clone the repo and set up the venv.
2. Configure Gunicorn as a systemd service.
3. Use Nginx as a reverse proxy to handle SSL and forward traffic to Gunicorn (port 5000).
4. **Important**: Ensure Nginx is configured to support WebSockets.

## 📁 Project Structure
- `app/`: Core application logic.
- `app/playwright_collector.py`: Data collection service.
- `app/provably_fair.py`: Cryptographic logic.
- `app/models.py`: Database schema.
- `wsgi.py`: Production server entry point.
- `Procfile`: Hosting process configuration.
# predictor.python
