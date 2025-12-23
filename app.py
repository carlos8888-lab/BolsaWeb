import os
from flask import Flask, render_template, redirect, url_for, flash, request, abort
from flask_login import LoginManager, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash

from models import db, User, Post
from forms import RegisterForm, LoginForm, PostForm

from pathlib import Path

def create_app():
    app = Flask(__name__)

    app.config["SECRET_KEY"] = load_secret_key()

    # DB: si no hay DATABASE_URL, usa SQLite local
    app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL", "sqlite:///database.db")
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    # Cookies de sesión más seguras
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = True  # Replit sirve en HTTPS

    db.init_app(app)

    login_manager = LoginManager()
    login_manager.init_app(app)
    login_manager.login_view = "index"

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    # Crear tablas si no existen
    with app.app_context():
        db.create_all()

    @app.get("/")
    def index():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))
        login_form = LoginForm()
        register_form = RegisterForm()
        return render_template("index.html", login_form=login_form, register_form=register_form)

    @app.post("/login")
    def login():
        form = LoginForm()
        if not form.validate_on_submit():
            flash("Datos inválidos.")
            return redirect(url_for("index"))

        user = User.query.filter_by(email=form.email.data.lower().strip()).first()
        if not user or not check_password_hash(user.password_hash, form.password.data):
            flash("Email o contraseña incorrectos.")
            return redirect(url_for("index"))

        login_user(user)
        return redirect(url_for("dashboard"))

    @app.post("/register")
    def register():
        form = RegisterForm()
        if not form.validate_on_submit():
            flash("Revisa el email y la contraseña (mínimo 10 caracteres).")
            return redirect(url_for("index"))

        email = form.email.data.lower().strip()
        if User.query.filter_by(email=email).first():
            flash("Ese email ya está registrado.")
            return redirect(url_for("index"))

        password_hash = generate_password_hash(form.password.data)
        user = User(
            email=email,
            username=(form.username.data.strip() if form.username.data else None),
            password_hash=password_hash,
        )
        db.session.add(user)
        db.session.commit()

        flash("Usuario creado. Ya puedes iniciar sesión.")
        return redirect(url_for("index"))

    @app.get("/logout")
    @login_required
    def logout():
        logout_user()
        return redirect(url_for("index"))

    @app.get("/dashboard")
    @login_required
    def dashboard():
        return render_template("dashboard.html")

    # --- POSTS (opcional) ---
    @app.route("/posts", methods=["GET", "POST"])
    @login_required
    def posts():
        form = PostForm()
        if form.validate_on_submit():
            p = Post(title=form.title.data, body=form.body.data, user_id=current_user.id)
            db.session.add(p)
            db.session.commit()
            return redirect(url_for("posts"))

        all_posts = Post.query.order_by(Post.created_at.desc()).all()
        return render_template("posts.html", form=form, posts=all_posts)

    return app

def load_secret_key() -> str:
    """
    Orden:
    1) Archivo local (fuera del repo) -> ideal para desarrollo en PC
    2) Variable de entorno SECRET_KEY -> ideal para Replit/producción
    3) Si no hay nada -> error (no se debe arrancar sin SECRET_KEY)
    """
    # 1) Ruta configurable por variable de entorno (por si cambias de PC)
    secret_file = os.environ.get("SECRET_KEY_FILE")

    # Si no está definida, intenta una ruta por defecto en el HOME del usuario
    if not secret_file:
        secret_file = str(r"D:\programacion\no copiar\bolsaweb.key")

    try:
        p = Path(secret_file)
        if p.exists():
            key = p.read_text(encoding="utf-8").strip()
            if key:
                return key
    except Exception:
        # Si falla leer el archivo, seguimos al fallback del entorno
        pass

    # 2) Fallback a entorno (Replit)
    key = os.environ.get("SECRET_KEY")
    if key:
        return key

    # 3) No arrancar sin clave
    raise RuntimeError(
        "Falta SECRET_KEY. Define SECRET_KEY (Replit Secrets) o crea el archivo de clave local."
    )

app = create_app()

if __name__ == "__main__":
    app.run(debug=True)
