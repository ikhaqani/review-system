from flask import Flask
from config import Config
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    db.init_app(app)

    # Registreer de routes
    from app.routes import bp as main_bp
    app.register_blueprint(main_bp)

    # Maak de database aan als deze nog niet bestaat
    with app.app_context():
        db.create_all()

    return app