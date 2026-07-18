from flask import Blueprint, render_template

admin_blueprint = Blueprint(
    "admin_web",
    __name__,
    template_folder="templates",
    static_folder="static",
    static_url_path="/admin-static",
)

ADMIN_PAGES = {
    "": ("Boshqaruv markazi", "dashboard"),
    "payments": ("Chek arizalari", "payments"),
    "orders": ("Buyurtmalar", "orders"),
    "pricing": ("Narxlar", "pricing"),
    "providers": ("Providerlar", "providers"),
    "users": ("Foydalanuvchilar", "users"),
    "admins": ("Adminlar", "admins"),
    "farm": ("Ferma nazorati", "farm"),
    "bonuses": ("Bonuslar", "bonuses"),
    "ranking": ("Reyting", "ranking"),
    "audit": ("Audit", "audit"),
    "real-sales": ("Real savdo", "real-sales"),
    "settings": ("Sozlamalar", "settings"),
}


@admin_blueprint.route("/admin", defaults={"page": ""})
@admin_blueprint.route("/admin/<page>")
def page(page: str):
    if page not in ADMIN_PAGES:
        return (
            render_template("common/error.html", code=404, message="Sahifa topilmadi", admin=True),
            404,
        )
    title, view = ADMIN_PAGES[page]
    return render_template("admin/page.html", title=title, view=view, current=page)
