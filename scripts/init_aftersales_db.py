import sys
import psycopg
from psycopg.rows import dict_row

DB_HOST = "127.0.0.1"
DB_PORT = 5433
DB_USER = "root"
DB_PASSWORD = "123456"
TARGET_DB = "ecommerce_aftersales"

ROOT_CONN_STR = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/postgres"
TARGET_CONN_STR = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{TARGET_DB}"

def create_database():
    print(f"Connecting to default database 'postgres' to check '{TARGET_DB}'...")
    with psycopg.connect(ROOT_CONN_STR, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (TARGET_DB,))
            if not cur.fetchone():
                cur.execute(f"CREATE DATABASE {TARGET_DB}")
                print(f"-> Database '{TARGET_DB}' created successfully.")
            else:
                print(f"-> Database '{TARGET_DB}' already exists.")

def init_tables_and_data():
    print(f"Connecting to '{TARGET_DB}' to create tables and insert seed data...")
    with psycopg.connect(TARGET_CONN_STR, autocommit=True) as conn:
        with conn.cursor() as cur:
            # 1. 创建表结构
            cur.execute("""
            -- 用户表
            CREATE TABLE IF NOT EXISTS users (
                user_id VARCHAR(64) PRIMARY KEY,
                username VARCHAR(100) NOT NULL,
                phone VARCHAR(20) NOT NULL,
                vip_level INT DEFAULT 1,
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            );

            -- 订单主表
            CREATE TABLE IF NOT EXISTS orders (
                order_id VARCHAR(64) PRIMARY KEY,
                user_id VARCHAR(64) REFERENCES users(user_id),
                order_status VARCHAR(32) NOT NULL, -- PAID, SHIPPED, DELIVERED, CANCELLED, CLOSED
                total_amount NUMERIC(10, 2) NOT NULL,
                pay_time TIMESTAMPTZ NOT NULL,
                delivery_time TIMESTAMPTZ, -- 签收时间
                shipping_address TEXT,
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            );

            -- 订单商品明细表
            CREATE TABLE IF NOT EXISTS order_items (
                item_id VARCHAR(64) PRIMARY KEY,
                order_id VARCHAR(64) REFERENCES orders(order_id),
                sku_id VARCHAR(64) NOT NULL,
                product_name VARCHAR(255) NOT NULL,
                category VARCHAR(64) NOT NULL, -- CLOTHING, FRESH_FOOD, DIGITAL, LUXURY
                unit_price NUMERIC(10, 2) NOT NULL,
                quantity INT NOT NULL DEFAULT 1,
                after_sales_status VARCHAR(32) DEFAULT 'NONE', -- NONE, IN_PROGRESS, REFUNDED, REJECTED
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            );

            -- 品类售后政策表 (用于规则引擎/政策核验)
            CREATE TABLE IF NOT EXISTS after_sales_policies (
                id SERIAL PRIMARY KEY,
                category VARCHAR(64) UNIQUE NOT NULL,
                category_name VARCHAR(100) NOT NULL,
                support_no_reason_days INT NOT NULL DEFAULT 7, -- 7天无理由
                support_quality_issue_days INT NOT NULL DEFAULT 30, -- 质量问题退换期
                need_return_goods BOOLEAN NOT NULL DEFAULT TRUE, -- 是否必须退回实物
                max_auto_refund_amount NUMERIC(10, 2) NOT NULL DEFAULT 300.00, -- 自动退款上限，超限转人工
                description TEXT
            );

            -- 物流追踪表
            CREATE TABLE IF NOT EXISTS logistics (
                tracking_number VARCHAR(64) PRIMARY KEY,
                order_id VARCHAR(64) REFERENCES orders(order_id),
                carrier VARCHAR(64) NOT NULL, -- 顺丰速运, 中通快递, 京东物流
                status VARCHAR(32) NOT NULL, -- DELIVERED, IN_TRANSIT, EXCEPTION
                signed_time TIMESTAMPTZ,
                current_location VARCHAR(255),
                updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            );

            -- 售后工单表
            CREATE TABLE IF NOT EXISTS after_sales_tickets (
                ticket_id VARCHAR(64) PRIMARY KEY,
                order_id VARCHAR(64) REFERENCES orders(order_id),
                item_id VARCHAR(64) REFERENCES order_items(item_id),
                user_id VARCHAR(64) REFERENCES users(user_id),
                type VARCHAR(32) NOT NULL, -- REFUND_ONLY(仅退款), RETURN_AND_REFUND(退货退款), EXCHANGE(换货)
                reason VARCHAR(255) NOT NULL,
                status VARCHAR(32) NOT NULL, -- PENDING_REVIEW, APPROVED_AUTO, NEED_RETURN_SHIPPING, REFUNDING, COMPLETED, ESCALATED_HUMAN, REJECTED
                refund_amount NUMERIC(10, 2) NOT NULL,
                return_tracking_no VARCHAR(64),
                human_reviewer VARCHAR(64),
                review_notes TEXT,
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            );

            -- 客户触达通知表
            CREATE TABLE IF NOT EXISTS notifications (
                notification_id SERIAL PRIMARY KEY,
                ticket_id VARCHAR(64),
                user_id VARCHAR(64),
                channel VARCHAR(32) NOT NULL, -- SMS, IN_APP
                title VARCHAR(128) NOT NULL,
                content TEXT NOT NULL,
                status VARCHAR(32) DEFAULT 'SENT',
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            );
            """)
            print("-> All tables verified / created.")

            # 2. 清理旧测试数据并插入结构化测试用例
            cur.execute("""
            TRUNCATE TABLE notifications, after_sales_tickets, logistics, order_items, orders, users, after_sales_policies CASCADE;

            -- 插入品类售后政策
            INSERT INTO after_sales_policies (category, category_name, support_no_reason_days, support_quality_issue_days, need_return_goods, max_auto_refund_amount, description)
            VALUES
            ('CLOTHING', '服装鞋帽', 7, 30, TRUE, 500.00, '支持7天无理由退货(需吊牌完整不影响二次销售)，破损或质量问题30天内支持退货退款并商家承担运费'),
            ('FRESH_FOOD', '生鲜水果', 0, 2, FALSE, 200.00, '生鲜不支持无理由退货；签收48小时内破损腐烂坏果包赔，仅退款无需退回商品'),
            ('DIGITAL', '数码配件', 7, 15, TRUE, 1000.00, '数码配件支持7天无理由退货，性能故障15天内免费换新'),
            ('LUXURY', '高档珠宝奢侈品', 0, 7, TRUE, 0.00, '贵重商品不支持自动退款，任何退换货诉求必须由高级专员人工审核');

            -- 插入用户数据
            INSERT INTO users (user_id, username, phone, vip_level)
            VALUES
            ('USR_001', '张三', '13800138001', 2),
            ('USR_002', '李四', '13900139002', 1),
            ('USR_003', '王五', '13700137003', 3);

            -- 插入订单与物流测试数据 (涵盖不同业务分支场景)
            -- Case 1: 完美匹配用户提问（衣服破损退货），签收2天，符合质量退换
            INSERT INTO orders (order_id, user_id, order_status, total_amount, pay_time, delivery_time, shipping_address)
            VALUES ('ORD_CLOTH_001', 'USR_001', 'DELIVERED', 199.00, NOW() - INTERVAL '4 days', NOW() - INTERVAL '2 days', '北京市朝阳区科技园区8号楼');
            INSERT INTO order_items (item_id, order_id, sku_id, product_name, category, unit_price, quantity)
            VALUES ('ITEM_CLOTH_001', 'ORD_CLOTH_001', 'SKU_1001', '纯棉修身连帽卫衣 (黑色 XL码)', 'CLOTHING', 199.00, 1);
            INSERT INTO logistics (tracking_number, order_id, carrier, status, signed_time, current_location)
            VALUES ('SF10010001', 'ORD_CLOTH_001', '顺丰速运', 'DELIVERED', NOW() - INTERVAL '2 days', '已签收 (菜鸟驿站代收)');

            -- Case 2: 7天无理由退货（鞋子尺码不合），签收3天
            INSERT INTO orders (order_id, user_id, order_status, total_amount, pay_time, delivery_time, shipping_address)
            VALUES ('ORD_CLOTH_002', 'USR_001', 'DELIVERED', 299.00, NOW() - INTERVAL '5 days', NOW() - INTERVAL '3 days', '北京市朝阳区科技园区8号楼');
            INSERT INTO order_items (item_id, order_id, sku_id, product_name, category, unit_price, quantity)
            VALUES ('ITEM_CLOTH_002', 'ORD_CLOTH_002', 'SKU_1002', '轻便透气减震跑步鞋 (42码)', 'CLOTHING', 299.00, 1);
            INSERT INTO logistics (tracking_number, order_id, carrier, status, signed_time, current_location)
            VALUES ('ZT20020002', 'ORD_CLOTH_002', '中通快递', 'DELIVERED', NOW() - INTERVAL '3 days', '已签收 (本人签收)');

            -- Case 3: 生鲜破损（车厘子烂果），签收1天，支持仅退款无需退回
            INSERT INTO orders (order_id, user_id, order_status, total_amount, pay_time, delivery_time, shipping_address)
            VALUES ('ORD_FRESH_003', 'USR_002', 'DELIVERED', 68.00, NOW() - INTERVAL '2 days', NOW() - INTERVAL '1 day', '上海市浦东新区陆家嘴金融街2号');
            INSERT INTO order_items (item_id, order_id, sku_id, product_name, category, unit_price, quantity)
            VALUES ('ITEM_FRESH_003', 'ORD_FRESH_003', 'SKU_2001', '智利进口车厘子J级 500g', 'FRESH_FOOD', 68.00, 1);
            INSERT INTO logistics (tracking_number, order_id, carrier, status, signed_time, current_location)
            VALUES ('JD30030003', 'ORD_FRESH_003', '京东物流', 'DELIVERED', NOW() - INTERVAL '1 day', '已签收 (前台代收)');

            -- Case 4: 超期订单（签收40天前），超出退货时效
            INSERT INTO orders (order_id, user_id, order_status, total_amount, pay_time, delivery_time, shipping_address)
            VALUES ('ORD_EXPIRED_004', 'USR_003', 'DELIVERED', 159.00, NOW() - INTERVAL '50 days', NOW() - INTERVAL '40 days', '深圳市南山区高新南道5号');
            INSERT INTO order_items (item_id, order_id, sku_id, product_name, category, unit_price, quantity)
            VALUES ('ITEM_EXPIRED_004', 'ORD_EXPIRED_004', 'SKU_1003', '基础款水洗牛仔裤 (32码)', 'CLOTHING', 159.00, 1);
            INSERT INTO logistics (tracking_number, order_id, carrier, status, signed_time, current_location)
            VALUES ('YT40040004', 'ORD_EXPIRED_004', '圆通速递', 'DELIVERED', NOW() - INTERVAL '40 days', '已签收 (蜂巢自提柜)');

            -- Case 5: 贵重高额订单（黄金项链 8800元），超出自动审核上限，触发转人工介入
            INSERT INTO orders (order_id, user_id, order_status, total_amount, pay_time, delivery_time, shipping_address)
            VALUES ('ORD_LUXURY_005', 'USR_003', 'DELIVERED', 8800.00, NOW() - INTERVAL '3 days', NOW() - INTERVAL '1 day', '深圳市南山区高新南道5号');
            INSERT INTO order_items (item_id, order_id, sku_id, product_name, category, unit_price, quantity)
            VALUES ('ITEM_LUXURY_005', 'ORD_LUXURY_005', 'SKU_3001', '足金999四叶草黄金吊坠项链 15g', 'LUXURY', 8800.00, 1);
            INSERT INTO logistics (tracking_number, order_id, carrier, status, signed_time, current_location)
            VALUES ('SF50050005', 'ORD_LUXURY_005', '顺丰特快', 'DELIVERED', NOW() - INTERVAL '1 day', '已签收 (本人人脸核验签收)');
            -- Case 6: 在途包裹（未签收，运输中，支持物流拦截）
            INSERT INTO orders (order_id, user_id, order_status, total_amount, pay_time, delivery_time, shipping_address)
            VALUES ('ORD_TRANSIT_006', 'USR_001', 'SHIPPED', 399.00, NOW() - INTERVAL '1 day', NULL, '北京市海淀区中关村南大街1号');
            INSERT INTO order_items (item_id, order_id, sku_id, product_name, category, unit_price, quantity)
            VALUES ('ITEM_TRANSIT_006', 'ORD_TRANSIT_006', 'SKU_1004', '商务休闲西装夹克 (藏青色 L码)', 'CLOTHING', 399.00, 1);
            INSERT INTO logistics (tracking_number, order_id, carrier, status, signed_time, current_location)
            VALUES ('SF60060006', 'ORD_TRANSIT_006', '顺丰速运', 'IN_TRANSIT', NULL, '北京市海淀区中关村集散中心，在途运输');
            """)
            print("-> Seed data inserted successfully.")

def init_langgraph_checkpoint_tables():
    print(f"Setting up LangGraph Postgres checkpointer tables in '{TARGET_DB}'...")
    from langgraph.checkpoint.postgres import PostgresSaver
    # PostgresSaver 具备 setup() 方法自动建表 checkpoints/checkpoint_blobs/checkpoint_writes
    with PostgresSaver.from_conn_string(TARGET_CONN_STR) as saver:
        saver.setup()
    print("-> LangGraph checkpoint tables created successfully.")

if __name__ == "__main__":
    create_database()
    init_tables_and_data()
    init_langgraph_checkpoint_tables()
    print("\n[SUCCESS] PostgreSQL Database & LangGraph tables initialized completely!")
