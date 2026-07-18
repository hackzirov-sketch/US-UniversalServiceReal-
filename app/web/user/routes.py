from flask import Blueprint, render_template

user_blueprint = Blueprint(
    "user_web",
    __name__,
    template_folder="templates",
    static_folder="static",
    static_url_path="/app-static",
)

USER_PAGES = {
    "": ("Bosh sahifa", "dashboard"),
    "stars": ("Telegram Stars", "catalog-stars"),
    "premium": ("Telegram Premium", "catalog-premium"),
    "gifts": ("Telegram Gifts", "catalog-gift"),
    "balance": ("Hisobim", "balance"),
    "topup": ("Hisob to‘ldirish", "topup"),
    "orders": ("Buyurtmalarim", "orders"),
    "farm": ("Pixel ferma", "farm"),
    "bonuses": ("Bonuslarim", "bonuses"),
    "points": ("Ballarim", "points"),
    "ranking": ("Reyting", "ranking"),
    "profile": ("Profil", "profile"),
    "help": ("Yordam", "help"),
}


@user_blueprint.route("/app", defaults={"page": ""})
@user_blueprint.route("/app/<page>")
def page(page: str):
    if page not in USER_PAGES:
        return render_template("common/error.html", code=404, message="Sahifa topilmadi"), 404
    title, view = USER_PAGES[page]
    return render_template("user/page.html", title=title, view=view, current=page)
