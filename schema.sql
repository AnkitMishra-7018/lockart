-- ============================================================
-- Local Seller E-Commerce Platform - Database v4 (17 tables)
-- Requires MySQL 8.0.16 or newer (needed for CHECK rules)
-- ============================================================

CREATE DATABASE IF NOT EXISTS local_market;
USE local_market;

-- ---------- 1. ADMINS ----------
CREATE TABLE admins (
    admin_id      INT PRIMARY KEY AUTO_INCREMENT,
    full_name     VARCHAR(100) NOT NULL,
    email         VARCHAR(100) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ---------- 2. CUSTOMERS ----------
CREATE TABLE customers (
    customer_id   INT PRIMARY KEY AUTO_INCREMENT,
    full_name     VARCHAR(100) NOT NULL,
    email         VARCHAR(100) NOT NULL UNIQUE,
    phone         VARCHAR(15)  NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    address       TEXT         NOT NULL,
    latitude      DECIMAL(10,8),
    longitude     DECIMAL(11,8),
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ---------- 3. SELLERS ----------
CREATE TABLE sellers (
    seller_id     INT PRIMARY KEY AUTO_INCREMENT,
    shop_name     VARCHAR(100) NOT NULL,
    owner_name    VARCHAR(100) NOT NULL,
    email         VARCHAR(100) NOT NULL UNIQUE,
    phone         VARCHAR(15)  NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    shop_address  TEXT         NOT NULL,
    latitude      DECIMAL(10,8),
    longitude     DECIMAL(11,8),
    status        ENUM('pending','approved','blocked') NOT NULL DEFAULT 'pending',
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ---------- 4. PRODUCTS ----------
CREATE TABLE products (
    product_id     INT PRIMARY KEY AUTO_INCREMENT,
    seller_id      INT NOT NULL,
    product_name   VARCHAR(150)  NOT NULL,
    category       VARCHAR(100)  NOT NULL,
    description    TEXT,
    price          DECIMAL(10,2) NOT NULL CHECK (price >= 0),
    stock_quantity INT           NOT NULL DEFAULT 0 CHECK (stock_quantity >= 0),
    is_active      BOOLEAN       NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (seller_id) REFERENCES sellers(seller_id)
);

-- ---------- 5. PRODUCT_IMAGES ----------
CREATE TABLE product_images (
    image_id   INT PRIMARY KEY AUTO_INCREMENT,
    product_id INT NOT NULL,
    image_path VARCHAR(255) NOT NULL,
    FOREIGN KEY (product_id) REFERENCES products(product_id)
);

-- ---------- 6. ORDERS ----------
CREATE TABLE orders (
    order_id           INT PRIMARY KEY AUTO_INCREMENT,
    customer_id        INT NOT NULL,
    seller_id          INT NOT NULL,
    total_amount       DECIMAL(10,2) NOT NULL,
    delivery_address   TEXT NOT NULL,
    delivery_latitude  DECIMAL(10,8),
    delivery_longitude DECIMAL(11,8),
    status ENUM('pending','accepted','packed','out_for_delivery','delivered','cancelled')
           NOT NULL DEFAULT 'pending',
    order_date   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    delivered_at DATETIME NULL,
    FOREIGN KEY (customer_id) REFERENCES customers(customer_id),
    FOREIGN KEY (seller_id)   REFERENCES sellers(seller_id)
);

-- ---------- 7. ORDER_ITEMS ----------
CREATE TABLE order_items (
    order_item_id INT PRIMARY KEY AUTO_INCREMENT,
    order_id      INT NOT NULL,
    product_id    INT NOT NULL,
    quantity      INT NOT NULL CHECK (quantity > 0),
    unit_price    DECIMAL(10,2) NOT NULL,   -- price at the time of purchase
    FOREIGN KEY (order_id)   REFERENCES orders(order_id),
    FOREIGN KEY (product_id) REFERENCES products(product_id)
);

-- ---------- 8. PRODUCT_REVIEWS ----------
CREATE TABLE product_reviews (
    review_id   INT PRIMARY KEY AUTO_INCREMENT,
    customer_id INT NOT NULL,
    product_id  INT NOT NULL,
    rating      INT NOT NULL CHECK (rating BETWEEN 1 AND 5),
    review_text TEXT,
    review_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (customer_id, product_id),       -- one review per customer per product
    FOREIGN KEY (customer_id) REFERENCES customers(customer_id),
    FOREIGN KEY (product_id)  REFERENCES products(product_id)
);

-- ---------- 9. SELLER_REVIEWS ----------
CREATE TABLE seller_reviews (
    review_id   INT PRIMARY KEY AUTO_INCREMENT,
    customer_id INT NOT NULL,
    seller_id   INT NOT NULL,
    order_id    INT NOT NULL,
    rating      INT NOT NULL CHECK (rating BETWEEN 1 AND 5),
    review_text TEXT,
    review_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (order_id),                      -- one seller review per order
    FOREIGN KEY (customer_id) REFERENCES customers(customer_id),
    FOREIGN KEY (seller_id)   REFERENCES sellers(seller_id),
    FOREIGN KEY (order_id)    REFERENCES orders(order_id)
);

-- ---------- 10. COMPLAINTS ----------
CREATE TABLE complaints (
    complaint_id   INT PRIMARY KEY AUTO_INCREMENT,
    customer_id    INT NOT NULL,
    seller_id      INT NOT NULL,
    order_id       INT NOT NULL,
    complaint_type ENUM('late_delivery','wrong_product','damaged_product',
                        'seller_misconduct','other') NOT NULL,
    complaint_text TEXT NOT NULL,
    status         ENUM('pending','resolved','rejected') NOT NULL DEFAULT 'pending',
    complaint_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (customer_id) REFERENCES customers(customer_id),
    FOREIGN KEY (seller_id)   REFERENCES sellers(seller_id),
    FOREIGN KEY (order_id)    REFERENCES orders(order_id)
);

-- ---------- 11. DELIVERY_GROUPS ----------
CREATE TABLE delivery_groups (
    group_id    INT PRIMARY KEY AUTO_INCREMENT,
    seller_id   INT NOT NULL,
    group_date  DATE NOT NULL,
    order_count INT NOT NULL DEFAULT 0,
    status      ENUM('planned','dispatched','completed') NOT NULL DEFAULT 'planned',
    FOREIGN KEY (seller_id) REFERENCES sellers(seller_id)
);

-- ---------- 12. GROUPED_ORDERS ----------
CREATE TABLE grouped_orders (
    grouped_order_id INT PRIMARY KEY AUTO_INCREMENT,
    group_id         INT NOT NULL,
    order_id         INT NOT NULL UNIQUE,   -- an order can be in only one group
    FOREIGN KEY (group_id) REFERENCES delivery_groups(group_id),
    FOREIGN KEY (order_id) REFERENCES orders(order_id)
);

-- ---------- 13. SELLER_METRICS ----------
-- Stores the customer-independent part of FVRS, once per seller per day.
-- Distance is added at search time because it depends on the customer.
CREATE TABLE seller_metrics (
    metric_id              INT PRIMARY KEY AUTO_INCREMENT,
    seller_id              INT NOT NULL,
    metric_date            DATE NOT NULL,
    avg_orders_last_7_days DECIMAL(10,2) NOT NULL DEFAULT 0,
    seller_rating          DECIMAL(5,2)  NOT NULL DEFAULT 3.50,
    completion_rate        DECIMAL(5,2)  NOT NULL DEFAULT 100,
    sales_volume           DECIMAL(12,2) NOT NULL DEFAULT 0,
    fairness_score         DECIMAL(5,2)  NOT NULL DEFAULT 0,
    rating_score           DECIMAL(5,2)  NOT NULL DEFAULT 0,
    sales_score            DECIMAL(5,2)  NOT NULL DEFAULT 0,
    base_score             DECIMAL(6,2)  NOT NULL DEFAULT 0,
    UNIQUE (seller_id, metric_date),        -- one row per seller per day
    FOREIGN KEY (seller_id) REFERENCES sellers(seller_id)
);

-- ---------- 14. IMAGE_STORE ----------
-- Seller-uploaded photos are kept here (inside the database), because the
-- hosting website's own folders are wiped on every restart.
CREATE TABLE image_store (
    image_id    INT PRIMARY KEY AUTO_INCREMENT,
    mime_type   VARCHAR(50) NOT NULL,
    image_data  MEDIUMBLOB  NOT NULL,
    uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ---------- 15. WISHLIST ----------
CREATE TABLE wishlist (
    customer_id INT NOT NULL,
    product_id  INT NOT NULL,
    added_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (customer_id, product_id),
    FOREIGN KEY (customer_id) REFERENCES customers(customer_id),
    FOREIGN KEY (product_id)  REFERENCES products(product_id)
);

-- ---------- 16. CONTACT_MESSAGES ----------
-- Messages sent through the Contact page.
CREATE TABLE contact_messages (
    message_id   INT PRIMARY KEY AUTO_INCREMENT,
    full_name    VARCHAR(100) NOT NULL,
    email        VARCHAR(100) NOT NULL,
    message_text TEXT NOT NULL,
    status       ENUM('new','read') NOT NULL DEFAULT 'new',
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ---------- 17. NEWSLETTER_SUBSCRIBERS ----------
CREATE TABLE newsletter_subscribers (
    subscriber_id INT PRIMARY KEY AUTO_INCREMENT,
    email         VARCHAR(100) NOT NULL UNIQUE,
    subscribed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);