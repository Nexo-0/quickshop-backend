from fastapi import FastAPI, APIRouter, HTTPException
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import MongoClient
from bson import ObjectId
import certifi
import ssl
import sys
import pymongo
import motor
import os
import logging
import re
from pathlib import Path
from pydantic import BaseModel, Field, ConfigDict
from typing import Any, Dict, List, Optional
import uuid
from datetime import datetime, timezone


ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# MongoDB connection
mongo_url = os.environ.get('MONGO_URL', 'mongodb://127.0.0.1:27017')
db_name = os.environ.get('DB_NAME', 'kiosk_db')

if 'NEW_USERNAME' in mongo_url or 'NEW_PASSWORD' in mongo_url or 'your_db_name' in mongo_url:
    logger.warning("MONGO_URL appears to contain placeholder values. Set a real MongoDB URI in backend/.env.")
if db_name == 'your_db_name':
    logger.warning("DB_NAME is still set to placeholder value 'your_db_name'.")

mongo_client_options: Dict[str, Any] = {
    "serverSelectionTimeoutMS": 10000,
    "connectTimeoutMS": 10000,
    "socketTimeoutMS": 10000,
}

# Atlas/SRV connections need explicit TLS CA configuration in some environments.
if mongo_url.startswith("mongodb+srv://"):
    mongo_client_options.update(
        {
            "tls": True,
            "tlsCAFile": certifi.where(),
        }
    )

# Synchronous PyMongo connector used for categories endpoints.
client = MongoClient(mongo_url, **mongo_client_options)
db = client[db_name]
collection = db["categories"]

# Async Motor connector used for the rest of async API handlers.
async_client = AsyncIOMotorClient(mongo_url, **mongo_client_options)
async_db = async_client[db_name]

DEFAULT_CATEGORIES = [
    {
        "name": "Burgers",
        "description": "Classic and specialty burgers",
        "image_url": "https://images.unsplash.com/photo-1568901346375-23c9450c58cd",
        "display_order": 1,
        "active": True,
    },
    {
        "name": "Drinks",
        "description": "Soft drinks, coffee, and refreshers",
        "image_url": "https://images.unsplash.com/photo-1511920170033-f8396924c348",
        "display_order": 2,
        "active": True,
    },
    {
        "name": "Desserts",
        "description": "Cakes, pastries, and sweet treats",
        "image_url": "https://images.unsplash.com/photo-1488477181946-6428a0291777",
        "display_order": 3,
        "active": True,
    },
    {
        "name": "Pizza",
        "description": "Hand-tossed pizzas and slices",
        "image_url": "https://images.unsplash.com/photo-1513104890138-7c749659a591",
        "display_order": 4,
        "active": True,
    },
]


def seed_categories_if_empty() -> None:
    """Insert default category data when the collection is empty."""
    existing_count = collection.count_documents({})
    if existing_count == 0:
        collection.insert_many(DEFAULT_CATEGORIES)
        logger.info("Seeded %d default categories into MongoDB.", len(DEFAULT_CATEGORIES))


def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-")


def _serialize_category(doc: Dict[str, Any]) -> Dict[str, Any]:
    normalized = dict(doc)
    if "_id" in normalized:
        normalized["_id"] = str(normalized["_id"])
    if "id" not in normalized and "_id" in normalized:
        normalized["id"] = normalized["_id"]
    return normalized

# Create the main app without a prefix
app = FastAPI()

# Create a router with the /api prefix
api_router = APIRouter(prefix="/api")


# ==================== DATA MODELS ====================

class Category(BaseModel):
    model_config = ConfigDict(extra="ignore")
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    description: str
    image_url: str
    display_order: int = 0
    active: bool = True


class CategoryCreate(BaseModel):
    name: str
    description: str
    image_url: str
    display_order: int = 0


class Product(BaseModel):
    model_config = ConfigDict(extra="ignore")
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    description: str
    price: float
    category_id: str
    image_url: str
    in_stock: bool = True
    active: bool = True


class ProductCreate(BaseModel):
    name: str
    description: str
    price: float
    category_id: str
    image_url: str


class OrderItem(BaseModel):
    product_id: str
    product_name: str
    quantity: int
    unit_price: float
    total_price: float


class Order(BaseModel):
    model_config = ConfigDict(extra="ignore")
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    order_number: str = Field(default_factory=lambda: f"ORD-{uuid.uuid4().hex[:8].upper()}")
    items: List[OrderItem]
    subtotal: float
    tax: float
    total: float
    payment_method: str
    status: str = "completed"
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class OrderCreate(BaseModel):
    items: List[OrderItem]
    subtotal: float
    tax: float
    total: float
    payment_method: str


# ==================== API ROUTES ====================

@api_router.get("/")
async def root():
    return {"message": "Kiosk System API Ready"}


@api_router.get("/health/connectors")
async def connectors_health():
    """Basic health check for external connectors (MongoDB)."""
    try:
        client.admin.command("ping")
        return {
            "status": "ok",
            "mongo": "connected",
            "db_name": db_name,
        }
    except Exception as exc:
        logger.exception("MongoDB connector health check failed")
        raise HTTPException(status_code=503, detail=f"MongoDB connection failed: {exc}")


# Categories Endpoints
@api_router.get("/categories")
def get_categories() -> List[Dict[str, Any]]:
    """Get all active categories ordered by display_order."""
    try:
        cursor = collection.find(
            {"$or": [{"active": True}, {"active": {"$exists": False}}]}
        ).sort("display_order", 1)
        categories = list(cursor)
        normalized_categories: List[Dict[str, Any]] = []
        for doc in categories:
            normalized = _serialize_category(doc)
            normalized_categories.append(normalized)

        return normalized_categories
    except Exception as exc:
        logger.exception("Failed to fetch categories from MongoDB")
        raise HTTPException(status_code=500, detail=f"Failed to fetch categories: {exc}") from exc


@api_router.get("/categories/{category_ref}")
def get_category(category_ref: str) -> Dict[str, Any]:
    """Get a single category by id, Mongo _id, or slug-like name (e.g. 'burgers')."""
    active_filter: Dict[str, Any] = {"$or": [{"active": True}, {"active": {"$exists": False}}]}
    try:
        category = collection.find_one({"id": category_ref, **active_filter})

        if not category and ObjectId.is_valid(category_ref):
            category = collection.find_one({"_id": ObjectId(category_ref), **active_filter})

        if not category:
            categories = list(collection.find(active_filter))
            target_slug = _slugify(category_ref)
            for doc in categories:
                if _slugify(doc.get("name", "")) == target_slug:
                    category = doc
                    break

        if not category:
            raise HTTPException(status_code=404, detail="Category not found")

        return _serialize_category(category)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to fetch category '%s'", category_ref)
        raise HTTPException(status_code=500, detail=f"Failed to fetch category: {exc}") from exc


@api_router.post("/categories", response_model=Category)
async def create_category(category_input: CategoryCreate):
    """Create a new category"""
    category = Category(**category_input.model_dump())
    doc = category.model_dump()
    await async_db.categories.insert_one(doc)
    return category


# Products Endpoints
@api_router.get("/products", response_model=List[Product])
async def get_products(category_id: Optional[str] = None):
    """Get all products, optionally filtered by category"""
    try:
        query: Dict[str, Any] = {
            "$or": [{"active": True}, {"active": {"$exists": False}}]
        }
        if category_id:
            query["category_id"] = category_id

        products = await async_db.products.find(query).to_list(2000)

        # Fallback: if the DB contains products without `active`, return them anyway.
        if not products:
            fallback_query: Dict[str, Any] = {}
            if category_id:
                fallback_query["category_id"] = category_id
            products = await async_db.products.find(fallback_query).to_list(2000)

        normalized_products: List[Dict[str, Any]] = []
        for product in products:
            normalized = dict(product)
            if "_id" in normalized:
                normalized["_id"] = str(normalized["_id"])
            if "id" not in normalized and "_id" in normalized:
                normalized["id"] = normalized["_id"]
            if "active" not in normalized:
                normalized["active"] = True
            if "in_stock" not in normalized:
                normalized["in_stock"] = True
            normalized_products.append(normalized)

        return normalized_products
    except Exception as exc:
        logger.exception("Failed to fetch products from MongoDB")
        raise HTTPException(status_code=500, detail=f"Failed to fetch products: {exc}") from exc


@api_router.get("/products/{product_id}", response_model=Product)
async def get_product(product_id: str):
    """Get a single product by ID"""
    product = await async_db.products.find_one({"id": product_id}, {"_id": 0})
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    return product


@api_router.post("/products", response_model=Product)
async def create_product(product_input: ProductCreate):
    """Create a new product"""
    product = Product(**product_input.model_dump())
    doc = product.model_dump()
    await async_db.products.insert_one(doc)
    return product


# Orders Endpoints
@api_router.post("/orders", response_model=Order)
async def create_order(order_input: OrderCreate):
    """Create a new order"""
    order = Order(**order_input.model_dump())
    doc = order.model_dump()
    doc['timestamp'] = doc['timestamp'].isoformat()
    
    await async_db.orders.insert_one(doc)
    return order


@api_router.get("/orders/{order_id}", response_model=Order)
async def get_order(order_id: str):
    """Get an order by ID"""
    order = await async_db.orders.find_one({"id": order_id}, {"_id": 0})
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    
    # Convert timestamp back to datetime if needed
    if isinstance(order['timestamp'], str):
        order['timestamp'] = datetime.fromisoformat(order['timestamp'])
    
    return order


@api_router.get("/orders", response_model=List[Order])
async def get_orders(limit: int = 50):
    """Get recent orders"""
    orders = await async_db.orders.find({}, {"_id": 0}).sort("timestamp", -1).limit(limit).to_list(limit)
    
    # Convert timestamps
    for order in orders:
        if isinstance(order['timestamp'], str):
            order['timestamp'] = datetime.fromisoformat(order['timestamp'])
    
    return orders


# Seed Data Endpoint (for development)
@api_router.post("/seed-data")
async def seed_data():
    """Seed the database with sample categories and products"""
    
    # Clear existing data
    await async_db.categories.delete_many({})
    await async_db.products.delete_many({})
    
    # Categories with images
    categories_data = [
        {
            "name": "Fast Food",
            "description": "Burgers, fries, and hot sandwiches",
            "image_url": "https://images.unsplash.com/photo-1568901346375-23c9450c58cd?crop=entropy&cs=srgb&fm=jpg&ixid=M3w4NjA1NTN8MHwxfHNlYXJjaHwxfHxidXJnZXJ8ZW58MHx8fHwxNzcxMjM2MzQ3fDA&ixlib=rb-4.1.0&q=85",
            "display_order": 1
        },
        {
            "name": "Beverages",
            "description": "Coffee, soft drinks, and juices",
            "image_url": "https://images.unsplash.com/photo-1511920170033-f8396924c348?crop=entropy&cs=srgb&fm=jpg&ixid=M3w4NjAzMjV8MHwxfHNlYXJjaHw0fHxjb2ZmZWV8ZW58MHx8fHwxNzcxMjM2MzUwfDA&ixlib=rb-4.1.0&q=85",
            "display_order": 2
        },
        {
            "name": "Snacks",
            "description": "Chips, candy, and quick bites",
            "image_url": "https://images.unsplash.com/photo-1599490659213-e2b9527bd087?crop=entropy&cs=srgb&fm=jpg&ixid=M3w4NjA1ODR8MHwxfHNlYXJjaHwyfHxjaGlwc3xlbnwwfHx8fDE3NzEyMzYzNTR8MA&ixlib=rb-4.1.0&q=85",
            "display_order": 3
        },
        {
            "name": "Electronics",
            "description": "Headphones, chargers, and accessories",
            "image_url": "https://images.unsplash.com/photo-1546435770-a3e426bf472b?crop=entropy&cs=srgb&fm=jpg&ixid=M3w3NTY2Nzh8MHwxfHNlYXJjaHwzfHxoZWFkcGhvbmVzfGVufDB8fHx8MTc3MTIzNjM1OHww&ixlib=rb-4.1.0&q=85",
            "display_order": 4
        },
        {
            "name": "Personal Care",
            "description": "Toiletries and hygiene products",
            "image_url": "https://images.unsplash.com/photo-1622866027662-14e3c5ee67e7?crop=entropy&cs=srgb&fm=jpg&ixid=M3w4NjAzMzJ8MHwxfHNlYXJjaHwyfHx0b2lsZXRyaWVzfGVufDB8fHx8MTc3MTIzNjM2M3ww&ixlib=rb-4.1.0&q=85",
            "display_order": 5
        },
        {
            "name": "Breakfast",
            "description": "Pastries, bagels, and morning favorites",
            "image_url": "https://images.unsplash.com/photo-1483695028939-5bb13f8648b0?crop=entropy&cs=srgb&fm=jpg&ixid=M3w4NjA0MTJ8MHwxfHNlYXJjaHwzfHxwYXN0cnl8ZW58MHx8fHwxNzcxMjM2MzY3fDA&ixlib=rb-4.1.0&q=85",
            "display_order": 6
        }
    ]
    
    # Insert categories and store IDs
    category_ids = {}
    for cat_data in categories_data:
        category = Category(**cat_data)
        doc = category.model_dump()
        await async_db.categories.insert_one(doc)
        category_ids[cat_data["name"]] = category.id
    
    # Products with images
    products_data = [
        # Fast Food
        {"name": "Classic Burger", "description": "Beef patty with lettuce, tomato, and cheese", "price": 8.99, "category": "Fast Food", "image_url": "https://images.unsplash.com/photo-1568901346375-23c9450c58cd?crop=entropy&cs=srgb&fm=jpg&ixid=M3w4NjA1NTN8MHwxfHNlYXJjaHwxfHxidXJnZXJ8ZW58MHx8fHwxNzcxMjM2MzQ3fDA&ixlib=rb-4.1.0&q=85"},
        {"name": "Cheeseburger", "description": "Double cheese with special sauce", "price": 9.99, "category": "Fast Food", "image_url": "https://images.unsplash.com/photo-1572802419224-296b0aeee0d9?crop=entropy&cs=srgb&fm=jpg&ixid=M3w4NjA1NTN8MHwxfHNlYXJjaHwzfHxidXJnZXJ8ZW58MHx8fHwxNzcxMjM2MzQ3fDA&ixlib=rb-4.1.0&q=85"},
        {"name": "Veggie Burger", "description": "Plant-based patty with fresh vegetables", "price": 8.49, "category": "Fast Food", "image_url": "https://images.unsplash.com/photo-1550547660-d9450f859349?crop=entropy&cs=srgb&fm=jpg&ixid=M3w4NjA1NTN8MHwxfHNlYXJjaHw0fHxidXJnZXJ8ZW58MHx8fHwxNzcxMjM2MzQ3fDA&ixlib=rb-4.1.0&q=85"},
        {"name": "Chicken Sandwich", "description": "Crispy chicken with mayo and pickles", "price": 7.99, "category": "Fast Food", "image_url": "https://images.unsplash.com/photo-1586190848861-99aa4a171e90?crop=entropy&cs=srgb&fm=jpg&ixid=M3w4NjA1NTN8MHwxfHNlYXJjaHwyfHxidXJnZXJ8ZW58MHx8fHwxNzcxMjM2MzQ3fDA&ixlib=rb-4.1.0&q=85"},
        {"name": "French Fries", "description": "Crispy golden fries with sea salt", "price": 3.99, "category": "Fast Food", "image_url": "https://images.unsplash.com/photo-1568901346375-23c9450c58cd?crop=entropy&cs=srgb&fm=jpg&ixid=M3w4NjA1NTN8MHwxfHNlYXJjaHwxfHxidXJnZXJ8ZW58MHx8fHwxNzcxMjM2MzQ3fDA&ixlib=rb-4.1.0&q=85"},
        
        # Beverages
        {"name": "Premium Coffee", "description": "Freshly brewed arabica coffee", "price": 3.49, "category": "Beverages", "image_url": "https://images.unsplash.com/photo-1511920170033-f8396924c348?crop=entropy&cs=srgb&fm=jpg&ixid=M3w4NjAzMjV8MHwxfHNlYXJjaHw0fHxjb2ZmZWV8ZW58MHx8fHwxNzcxMjM2MzUwfDA&ixlib=rb-4.1.0&q=85"},
        {"name": "Latte", "description": "Espresso with steamed milk", "price": 4.49, "category": "Beverages", "image_url": "https://images.unsplash.com/photo-1509042239860-f550ce710b93?crop=entropy&cs=srgb&fm=jpg&ixid=M3w4NjAzMjV8MHwxfHNlYXJjaHwyfHxjb2ZmZWV8ZW58MHx8fHwxNzcxMjM2MzUwfDA&ixlib=rb-4.1.0&q=85"},
        {"name": "Cola", "description": "Classic carbonated soft drink", "price": 2.49, "category": "Beverages", "image_url": "https://images.unsplash.com/photo-1625740822008-e45abf4e01d5?crop=entropy&cs=srgb&fm=jpg&ixid=M3w4NjA1MTN8MHwxfHNlYXJjaHwyfHxzb2Z0JTIwZHJpbmtzfGVufDB8fHx8MTc3MTIzNjM3MXww&ixlib=rb-4.1.0&q=85"},
        {"name": "Orange Juice", "description": "Freshly squeezed orange juice", "price": 3.99, "category": "Beverages", "image_url": "https://images.unsplash.com/photo-1527960471264-932f39eb5846?crop=entropy&cs=srgb&fm=jpg&ixid=M3w4NjA1MTN8MHwxfHNlYXJjaHwxfHxzb2Z0JTIwZHJpbmtzfGVufDB8fHx8MTc3MTIzNjM3MXww&ixlib=rb-4.1.0&q=85"},
        {"name": "Iced Tea", "description": "Refreshing lemon iced tea", "price": 2.99, "category": "Beverages", "image_url": "https://images.unsplash.com/photo-1511920170033-f8396924c348?crop=entropy&cs=srgb&fm=jpg&ixid=M3w4NjAzMjV8MHwxfHNlYXJjaHw0fHxjb2ZmZWV8ZW58MHx8fHwxNzcxMjM2MzUwfDA&ixlib=rb-4.1.0&q=85"},
        
        # Snacks
        {"name": "Potato Chips", "description": "Classic salted potato chips", "price": 2.49, "category": "Snacks", "image_url": "https://images.unsplash.com/photo-1599490659213-e2b9527bd087?crop=entropy&cs=srgb&fm=jpg&ixid=M3w4NjA1ODR8MHwxfHNlYXJjaHwyfHxjaGlwc3xlbnwwfHx8fDE3NzEyMzYzNTR8MA&ixlib=rb-4.1.0&q=85"},
        {"name": "Tortilla Chips", "description": "Crispy corn tortilla chips", "price": 2.99, "category": "Snacks", "image_url": "https://images.unsplash.com/photo-1613919113640-25732ec5e61f?crop=entropy&cs=srgb&fm=jpg&ixid=M3w4NjA1ODR8MHwxfHNlYXJjaHwxfHxjaGlwc3xlbnwwfHx8fDE3NzEyMzYzNTR8MA&ixlib=rb-4.1.0&q=85"},
        {"name": "Cheese Puffs", "description": "Cheesy flavored snack puffs", "price": 2.29, "category": "Snacks", "image_url": "https://images.unsplash.com/photo-1617102738820-bee2545405fd?crop=entropy&cs=srgb&fm=jpg&ixid=M3w4NjA1ODR8MHwxfHNlYXJjaHwzfHxjaGlwc3xlbnwwfHx8fDE3NzEyMzYzNTR8MA&ixlib=rb-4.1.0&q=85"},
        {"name": "Candy Bar", "description": "Chocolate and caramel candy bar", "price": 1.99, "category": "Snacks", "image_url": "https://images.unsplash.com/photo-1599490659213-e2b9527bd087?crop=entropy&cs=srgb&fm=jpg&ixid=M3w4NjA1ODR8MHwxfHNlYXJjaHwyfHxjaGlwc3xlbnwwfHx8fDE3NzEyMzYzNTR8MA&ixlib=rb-4.1.0&q=85"},
        
        # Electronics
        {"name": "Wireless Headphones", "description": "Bluetooth over-ear headphones", "price": 49.99, "category": "Electronics", "image_url": "https://images.unsplash.com/photo-1546435770-a3e426bf472b?crop=entropy&cs=srgb&fm=jpg&ixid=M3w3NTY2Nzh8MHwxfHNlYXJjaHwzfHxoZWFkcGhvbmVzfGVufDB8fHx8MTc3MTIzNjM1OHww&ixlib=rb-4.1.0&q=85"},
        {"name": "Earbuds", "description": "Compact wireless earbuds", "price": 29.99, "category": "Electronics", "image_url": "https://images.unsplash.com/photo-1505740420928-5e560c06d30e?crop=entropy&cs=srgb&fm=jpg&ixid=M3w3NTY2Nzh8MHwxfHNlYXJjaHwxfHxoZWFkcGhvbmVzfGVufDB8fHx8MTc3MTIzNjM1OHww&ixlib=rb-4.1.0&q=85"},
        {"name": "Phone Charger", "description": "Fast charging USB-C cable", "price": 14.99, "category": "Electronics", "image_url": "https://images.unsplash.com/photo-1725304382197-663ae3864750?crop=entropy&cs=srgb&fm=jpg&ixid=M3w4NjY2NjV8MHwxfHNlYXJjaHwzfHxwaG9uZSUyMGNoYXJnZXJ8ZW58MHx8fHwxNzcxMjM2Mzc1fDA&ixlib=rb-4.1.0&q=85"},
        {"name": "Portable Speaker", "description": "Bluetooth portable speaker", "price": 39.99, "category": "Electronics", "image_url": "https://images.unsplash.com/photo-1618366712010-f4ae9c647dcb?crop=entropy&cs=srgb&fm=jpg&ixid=M3w3NTY2Nzh8MHwxfHNlYXJjaHwyfHxoZWFkcGhvbmVzfGVufDB8fHx8MTc3MTIzNjM1OHww&ixlib=rb-4.1.0&q=85"},
        
        # Personal Care
        {"name": "Hand Sanitizer", "description": "Antibacterial hand sanitizer gel", "price": 3.99, "category": "Personal Care", "image_url": "https://images.unsplash.com/photo-1622866027662-14e3c5ee67e7?crop=entropy&cs=srgb&fm=jpg&ixid=M3w4NjAzMzJ8MHwxfHNlYXJjaHwyfHx0b2lsZXRyaWVzfGVufDB8fHx8MTc3MTIzNjM2M3ww&ixlib=rb-4.1.0&q=85"},
        {"name": "Toothbrush Kit", "description": "Travel toothbrush with toothpaste", "price": 5.99, "category": "Personal Care", "image_url": "https://images.unsplash.com/photo-1603990103103-baf3ada7af1c?crop=entropy&cs=srgb&fm=jpg&ixid=M3w4NjAzMzJ8MHwxfHNlYXJjaHwxfHx0b2lsZXRyaWVzfGVufDB8fHx8MTc3MTIzNjM2M3ww&ixlib=rb-4.1.0&q=85"},
        {"name": "Face Wipes", "description": "Refreshing cleansing wipes", "price": 4.49, "category": "Personal Care", "image_url": "https://images.unsplash.com/photo-1622866027662-14e3c5ee67e7?crop=entropy&cs=srgb&fm=jpg&ixid=M3w4NjAzMzJ8MHwxfHNlYXJjaHwyfHx0b2lsZXRyaWVzfGVufDB8fHx8MTc3MTIzNjM2M3ww&ixlib=rb-4.1.0&q=85"},
        {"name": "Deodorant", "description": "24-hour protection deodorant", "price": 6.99, "category": "Personal Care", "image_url": "https://images.unsplash.com/photo-1603990103103-baf3ada7af1c?crop=entropy&cs=srgb&fm=jpg&ixid=M3w4NjAzMzJ8MHwxfHNlYXJjaHwxfHx0b2lsZXRyaWVzfGVufDB8fHx8MTc3MTIzNjM2M3ww&ixlib=rb-4.1.0&q=85"},
        
        # Breakfast
        {"name": "Croissant", "description": "Buttery flaky croissant", "price": 3.49, "category": "Breakfast", "image_url": "https://images.unsplash.com/photo-1483695028939-5bb13f8648b0?crop=entropy&cs=srgb&fm=jpg&ixid=M3w4NjA0MTJ8MHwxfHNlYXJjaHwzfHxwYXN0cnl8ZW58MHx8fHwxNzcxMjM2MzY3fDA&ixlib=rb-4.1.0&q=85"},
        {"name": "Blueberry Muffin", "description": "Fresh baked blueberry muffin", "price": 2.99, "category": "Breakfast", "image_url": "https://images.unsplash.com/photo-1620980776848-84ac10194945?crop=entropy&cs=srgb&fm=jpg&ixid=M3w4NjA0MTJ8MHwxfHNlYXJjaHw0fHxwYXN0cnl8ZW58MHx8fHwxNzcxMjM2MzY3fDA&ixlib=rb-4.1.0&q=85"},
        {"name": "Bagel", "description": "Plain bagel with cream cheese", "price": 3.99, "category": "Breakfast", "image_url": "https://images.unsplash.com/photo-1623334044303-241021148842?crop=entropy&cs=srgb&fm=jpg&ixid=M3w4NjA0MTJ8MHwxfHNlYXJjaHwxfHxwYXN0cnl8ZW58MHx8fHwxNzcxMjM2MzY3fDA&ixlib=rb-4.1.0&q=85"},
        {"name": "Danish Pastry", "description": "Sweet fruit-filled danish", "price": 3.29, "category": "Breakfast", "image_url": "https://images.unsplash.com/photo-1483695028939-5bb13f8648b0?crop=entropy&cs=srgb&fm=jpg&ixid=M3w4NjA0MTJ8MHwxfHNlYXJjaHwzfHxwYXN0cnl8ZW58MHx8fHwxNzcxMjM2MzY3fDA&ixlib=rb-4.1.0&q=85"},
    ]
    
    # Insert products
    for prod_data in products_data:
        category_name = prod_data.pop("category")
        prod_data["category_id"] = category_ids[category_name]
        product = Product(**prod_data)
        doc = product.model_dump()
        await async_db.products.insert_one(doc)
    
    return {
        "message": "Database seeded successfully",
        "categories": len(categories_data),
        "products": len(products_data)
    }


# Include the router in the main app
app.include_router(api_router)

cors_origins = [origin.strip() for origin in os.environ.get('CORS_ORIGINS', '*').split(',') if origin.strip()]
if not cors_origins:
    cors_origins = ['*']

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("shutdown")
async def shutdown_db_client():
    async_client.close()
    client.close()


@app.on_event("startup")
async def startup_checks():
    try:
        logger.info("Python runtime: %s", sys.version.replace("\n", " "))
        logger.info("OpenSSL runtime: %s", ssl.OPENSSL_VERSION)
        logger.info("PyMongo version: %s | Motor version: %s", pymongo.__version__, motor.version)
        client.admin.command("ping")
        await async_db.command("ping")
        seed_categories_if_empty()
        logger.info("MongoDB connectors ready for database '%s'", db_name)
    except Exception as exc:
        logger.warning("MongoDB connector not ready at startup: %s", exc)
