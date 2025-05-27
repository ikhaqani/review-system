from flask import Flask
from config import Config
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager

db = SQLAlchemy()
login = LoginManager()
login.login_view = 'main.login' # Vertel Flask-Login welke route de loginpagina is

def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    db.init_app(app)
    login.init_app(app)

    # Registreer de routes
    from app.routes import bp as main_bp
    app.register_blueprint(main_bp)

    # Maak de database aan als deze nog niet bestaat
    with app.app_context():
        db.create_all()

    return app