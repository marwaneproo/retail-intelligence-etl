from pathlib import Path
import logging
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.ticker as mtick

BASE_DIR = Path(__file__).resolve().parent
SOURCE_FILE = BASE_DIR / "retail_supply_chain_raw_data.xlsx"
OUTPUT_DIR = BASE_DIR / "output"
FIGURE_DIR = OUTPUT_DIR / "figures"

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
plt.rcParams.update({
    "figure.figsize": (12, 6.5),
    "figure.dpi": 140,
    "savefig.dpi": 180,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.titleweight": "bold",
    "axes.titlesize": 15,
    "axes.labelcolor": "#475569",
    "xtick.color": "#64748b",
    "ytick.color": "#64748b",
    "font.family": "DejaVu Sans",
})

def load_source():
    "Extract all source tables from the workbook."
    if not SOURCE_FILE.exists():
        raise FileNotFoundError(f"Source workbook not found: {SOURCE_FILE}")
    xls = pd.ExcelFile(SOURCE_FILE)
    required = {"orders", "order_items", "products", "customers", "suppliers", "inventory_snapshots"}
    missing = required - set(xls.sheet_names)
    if missing:
        raise ValueError(f"Missing workbook sheets: {sorted(missing)}")
    tables = {name: pd.read_excel(xls, sheet_name=name) for name in required}
    logging.info("Extracted %s source tables", len(tables))
    return tables

def clean_sources(t):
    "Standardize types, remove duplicates, validate keys and business rules."
    quality = []
    def record(table, check, affected):
        quality.append({"table": table, "check": check, "affected_rows": int(affected)})

    orders = t["orders"].copy()
    before = len(orders)
    orders = orders.drop_duplicates(subset=["order_id"], keep="first")
    record("orders", "duplicate order_id removed", before-len(orders))
    orders["order_date"] = pd.to_datetime(orders["order_date"], errors="coerce")
    record("orders", "invalid order_date", orders["order_date"].isna().sum())
    orders = orders.dropna(subset=["order_id", "order_date"])
    orders["shipping_fee"] = pd.to_numeric(orders["shipping_fee"], errors="coerce").fillna(0).clip(lower=0)

    items = t["order_items"].copy()
    items["quantity"] = pd.to_numeric(items["quantity"], errors="coerce")
    items["unit_price"] = pd.to_numeric(items["unit_price"], errors="coerce")
    items["discount_rate"] = pd.to_numeric(items["discount_rate"], errors="coerce")
    before = len(items)
    items = items.dropna(subset=["order_id","product_id","quantity","unit_price"])
    items = items[(items["quantity"] > 0) & (items["unit_price"] > 0)]
    record("order_items", "missing/non-positive quantity or price removed", before-len(items))
    invalid_discount_count = ((items["discount_rate"] < 0) | (items["discount_rate"] > 0.80)).sum()
    items["discount_rate"] = items["discount_rate"].fillna(0).clip(0, 0.80)
    record("order_items", "discount rate capped to valid 0-80% range", invalid_discount_count)
    items = items.drop_duplicates()

    products = t["products"].copy()
    products["unit_cost"] = pd.to_numeric(products["unit_cost"], errors="coerce")
    products["list_price"] = pd.to_numeric(products["list_price"], errors="coerce")
    products = products.dropna(subset=["product_id","unit_cost","list_price"])
    products = products[(products["unit_cost"] >= 0) & (products["list_price"] > 0)]
    products = products.drop_duplicates("product_id")

    customers = t["customers"].copy().drop_duplicates("customer_id")
    customers["signup_date"] = pd.to_datetime(customers["signup_date"], errors="coerce")
    customers = customers.dropna(subset=["customer_id"])

    suppliers = t["suppliers"].copy().drop_duplicates("supplier_id")
    inventory = t["inventory_snapshots"].copy()
    inventory["snapshot_date"] = pd.to_datetime(inventory["snapshot_date"], errors="coerce")
    for c in ["on_hand_units","units_sold_last_30d","units_on_order"]:
        inventory[c] = pd.to_numeric(inventory[c], errors="coerce")
    inventory = inventory.dropna(subset=["snapshot_date","product_id"])
    inventory["on_hand_units"] = inventory["on_hand_units"].fillna(0).clip(lower=0)
    inventory["units_sold_last_30d"] = inventory["units_sold_last_30d"].fillna(0).clip(lower=0)
    inventory["units_on_order"] = inventory["units_on_order"].fillna(0).clip(lower=0)

    # Referential integrity: exclude orphan facts rather than silently inventing dimensions.
    valid_orders = set(orders.order_id)
    valid_products = set(products.product_id)
    before=len(items)
    items=items[items.order_id.isin(valid_orders) & items.product_id.isin(valid_products)]
    record("order_items", "orphan order/product keys removed", before-len(items))
    inventory=inventory[inventory.product_id.isin(valid_products)]
    return {
        "orders": orders, "order_items": items, "products": products,
        "customers": customers, "suppliers": suppliers, "inventory": inventory,
        "quality": pd.DataFrame(quality)
    }

def transform(clean):
    "Build a sales fact table and business-ready analytical marts."
    orders=clean["orders"]
    items=clean["order_items"]
    products=clean["products"]
    customers=clean["customers"]
    suppliers=clean["suppliers"]

    fact=items.merge(orders, on="order_id", how="inner", validate="many_to_one")
    fact=fact.merge(products, on="product_id", how="left", validate="many_to_one")
    fact=fact.merge(customers[["customer_id","region","customer_segment"]], on="customer_id", how="left", validate="many_to_one")
    fact=fact.merge(suppliers[["supplier_id","supplier_name","lead_time_days"]], on="supplier_id", how="left", validate="many_to_one")
    fact["region"]=fact["region"].fillna("Unknown")
    fact["gross_sales"]=fact["quantity"]*fact["unit_price"]
    fact["discount_amount"]=fact["gross_sales"]*fact["discount_rate"]
    fact["net_sales"]=fact["gross_sales"]-fact["discount_amount"]
    fact["estimated_cost"]=fact["quantity"]*fact["unit_cost"]
    fact["gross_profit"]=fact["net_sales"]-fact["estimated_cost"]
    fact["gross_margin_pct"]=np.where(fact["net_sales"]>0, fact["gross_profit"]/fact["net_sales"], np.nan)
    fact["order_month"]=fact["order_date"].dt.to_period("M").astype(str)
    # Canceled orders and returned orders are excluded from recognized sales.
    valid=fact["order_status"].isin(["Delivered"])
    sales=fact.loc[valid].copy()

    monthly=(sales.groupby("order_month",as_index=False)
        .agg(revenue=("net_sales","sum"), gross_profit=("gross_profit","sum"),
             orders=("order_id","nunique"), units=("quantity","sum"),
             customers=("customer_id","nunique")))
    monthly["gross_margin_pct"]=monthly["gross_profit"]/monthly["revenue"]
    monthly["revenue_mom_pct"]=monthly["revenue"].pct_change()

    product=(sales.groupby(["product_id","product_name","category"],as_index=False)
        .agg(revenue=("net_sales","sum"), gross_profit=("gross_profit","sum"),
             units_sold=("quantity","sum"), orders=("order_id","nunique")))
    product["gross_margin_pct"]=product["gross_profit"]/product["revenue"]
    product=product.sort_values("revenue",ascending=False)

    region=(sales.groupby("region",as_index=False)
        .agg(revenue=("net_sales","sum"), gross_profit=("gross_profit","sum"),
             orders=("order_id","nunique"), customers=("customer_id","nunique")))
    region["gross_margin_pct"]=region["gross_profit"]/region["revenue"]
    region=region.sort_values("revenue",ascending=False)

    inv=clean["inventory"].merge(products[["product_id","product_name","category","supplier_id"]],on="product_id",how="left")
    inv=inv.sort_values("snapshot_date").drop_duplicates("product_id",keep="last")
    inv["avg_daily_demand"]=inv["units_sold_last_30d"]/30
    inv["days_of_cover"]=np.where(inv["avg_daily_demand"]>0,inv["on_hand_units"]/inv["avg_daily_demand"],np.inf)
    inv["inventory_status"]=np.select(
        [inv["on_hand_units"]<=0, inv["days_of_cover"]<7, inv["days_of_cover"]>60],
        ["Out of stock","Reorder urgently","Overstock risk"], default="Healthy")
    inv=inv.sort_values(["days_of_cover","on_hand_units"])

    return fact, sales, monthly, product, region, inv

def save_outputs(clean, fact, sales, monthly, product, region, inv):
    OUTPUT_DIR.mkdir(exist_ok=True)
    FIGURE_DIR.mkdir(parents=True,exist_ok=True)
    for name in ["orders","order_items","products","customers"]:
        clean[name].to_csv(OUTPUT_DIR/f"clean_{name}.csv",index=False)
    fact.to_csv(OUTPUT_DIR/"fact_sales_all_statuses.csv",index=False)
    sales.to_csv(OUTPUT_DIR/"fact_sales_delivered.csv",index=False)
    monthly.to_csv(OUTPUT_DIR/"mart_monthly_performance.csv",index=False)
    product.to_csv(OUTPUT_DIR/"mart_product_performance.csv",index=False)
    region.to_csv(OUTPUT_DIR/"mart_region_performance.csv",index=False)
    inv.to_csv(OUTPUT_DIR/"mart_inventory_risk.csv",index=False)
    clean["quality"].to_csv(OUTPUT_DIR/"data_quality_report.csv",index=False)

def make_charts(monthly, product, region, inv):
    # 1 Revenue & profit trend
    fig,ax=plt.subplots()
    x=pd.to_datetime(monthly["order_month"])
    ax.plot(x,monthly["revenue"],marker="o",linewidth=2.8,label="Net revenue")
    ax.plot(x,monthly["gross_profit"],marker="o",linewidth=2.2,label="Gross profit")
    ax.set_title("Revenue and gross profit — monthly trend",loc="left",pad=16)
    ax.set_ylabel("Amount (MAD)")
    ax.yaxis.set_major_formatter(mtick.FuncFormatter(lambda v,_: f"{v/1000:,.0f}k"))
    ax.grid(axis="y",alpha=.2); ax.legend(frameon=False,ncol=2)
    fig.autofmt_xdate(); fig.tight_layout(); fig.savefig(FIGURE_DIR/"01_monthly_revenue_profit.png"); plt.close(fig)

    # 2 Product portfolio: revenue vs margin
    top=product.head(35)
    fig,ax=plt.subplots()
    sizes=np.clip(top["units_sold"]*1.3,30,900)
    sc=ax.scatter(top["revenue"],top["gross_margin_pct"]*100,s=sizes,c=top["gross_profit"],alpha=.75,edgecolors="white",linewidth=.7)
    ax.axhline(product["gross_margin_pct"].median()*100,linestyle="--",alpha=.5)
    ax.set_title("Product portfolio — revenue vs gross margin",loc="left",pad=16)
    ax.set_xlabel("Net revenue (MAD)"); ax.set_ylabel("Gross margin (%)")
    ax.xaxis.set_major_formatter(mtick.FuncFormatter(lambda v,_: f"{v/1000:,.0f}k"))
    ax.grid(alpha=.18); fig.colorbar(sc,ax=ax,label="Gross profit (MAD)")
    fig.tight_layout(); fig.savefig(FIGURE_DIR/"02_product_portfolio.png"); plt.close(fig)

    # 3 Regional revenue and margin
    
    r=region.sort_values("revenue")
    fig,ax=plt.subplots()
    bars=ax.barh(r["region"],r["revenue"])
    ax.set_title("Regional performance — net revenue",loc="left",pad=16)
    ax.set_xlabel("Net revenue (MAD)")
    ax.xaxis.set_major_formatter(mtick.FuncFormatter(lambda v,_: f"{v/1000:,.0f}k"))
    ax.grid(axis="x",alpha=.18); ax.set_axisbelow(True)
    for b,val in zip(bars,r["revenue"]):
        ax.text(val, b.get_y()+b.get_height()/2, f"  {val:,.0f}",va="center",fontsize=8)
    fig.tight_layout(); fig.savefig(FIGURE_DIR/"03_regional_performance.png"); plt.close(fig)

    # 4 Inventory risk
    
    status=inv["inventory_status"].value_counts()
    fig,ax=plt.subplots()
    bars=ax.bar(status.index,status.values)
    ax.set_title("Inventory health — latest available snapshot",loc="left",pad=16)
    ax.set_ylabel("Number of products"); ax.grid(axis="y",alpha=.18); ax.set_axisbelow(True)
    for b,v in zip(bars,status.values): ax.text(b.get_x()+b.get_width()/2,v,f"{v}",ha="center",va="bottom")
    ax.tick_params(axis="x",rotation=12)
    fig.tight_layout(); fig.savefig(FIGURE_DIR/"04_inventory_health.png"); plt.close(fig)

    # 5 Category revenue mix
    
    cat=product.groupby("category",as_index=False)["revenue"].sum().sort_values("revenue")
    fig,ax=plt.subplots()
    ax.barh(cat["category"],cat["revenue"])
    ax.set_title("Revenue contribution by product category",loc="left",pad=16)
    ax.set_xlabel("Net revenue (MAD)")
    ax.xaxis.set_major_formatter(mtick.FuncFormatter(lambda v,_: f"{v/1000:,.0f}k"))
    ax.grid(axis="x",alpha=.18); ax.set_axisbelow(True)
    fig.tight_layout(); fig.savefig(FIGURE_DIR/"05_category_mix.png"); plt.close(fig)

def main():
    
    logging.info("Starting Retail Intelligence ETL")
    raw=load_source()
    clean=clean_sources(raw)
    fact,sales,monthly,product,region,inv=transform(clean)
    save_outputs(clean,fact,sales,monthly,product,region,inv)
    make_charts(monthly,product,region,inv)
    revenue=sales["net_sales"].sum()
    profit=sales["gross_profit"].sum()
    margin=profit/revenue if revenue else 0
    logging.info("Delivered revenue: %.2f MAD | Gross profit: %.2f MAD | Margin: %.1f%%",revenue,profit,margin*100)
    logging.info("Pipeline complete. Explore the output folder and figures subfolder.")

if __name__ == "__main__":
    main()
