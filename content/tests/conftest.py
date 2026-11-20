import pytest
from django.test import Client

from core.models import Building, Organization, TeamMember
from core.views import save_languages

ORDER_CONFIRMATION = {
    "key": "pavilion.order_confirmation",
    "name": "Order confirmation",
    "fires": "When an order is placed",
    "variables": {
        "customer.first_name": {"label": "first name", "sample": "Olena"},
        "order.number": {"label": "order number", "sample": "#0142"},
        "order.total": {"label": "order total", "sample": "$23.50"},
        "order.items": {
            "label": "order items",
            "list": {"name": {}, "quantity": {}, "price": {}},
            "sample": [
                {"name": "Borscht", "quantity": 2, "price": "$9.00"},
                {"name": "Varenyky", "quantity": 1, "price": "$11.00"},
                {"name": "Kompot", "quantity": 3, "price": "$3.50"},
            ],
        },
    },
    "content": {
        "en": {
            "email": {"subject": "Your order {order.number} is in", "blocks": [
                {"id": "h", "type": "heading", "text": "Thanks for your order, {customer.first_name}"},
                {"id": "items", "type": "repeat", "source": "order.items", "show": ["quantity", "name", "price"]},
            ]},
            "notification": {"text": "Order {order.number} is in."},
        },
    },
}


@pytest.fixture(autouse=True)
def maple_grove(db):
    """The fictional Maple Grove Community Centre, with three languages and an owner."""
    from django.core.cache import cache

    cache.clear()
    save_languages(["en", "uk", "fr"], "en")
    org = Organization.get()
    org.name = "Maple Grove Community Centre"
    org.mailing_address = "1200 Alder Street, Vancouver, BC"
    org.setup_complete = True
    org.save()
    TeamMember.objects.create_user("owner@maplegrove.example", "long password", role="owner", name="Andrew")
    return org


def _client(role):
    user = TeamMember.objects.create_user(f"{role}@maplegrove.example", "long password", role=role, name=role.title())
    client = Client()
    client.force_login(user)
    client.user = user
    return client


@pytest.fixture
def editor_client():
    return _client("editor")


@pytest.fixture
def viewer_client():
    return _client("viewer")


@pytest.fixture
def pavilion():
    return Building.objects.create(name="Pavilion", kind="pavilion")


@pytest.fixture
def order_definition():
    import copy

    return copy.deepcopy(ORDER_CONFIRMATION)
