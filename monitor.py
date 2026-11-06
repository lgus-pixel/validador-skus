#!/usr/bin/env python3
"""
Monitor masivo de links + SKU de Plaza Vea y Makro (plataforma VTEX).

Uso:
    python3 monitor.py entrada.csv|entrada.xlsx [-o salida.xlsx] [-w 8]

Entrada (columnas, nombres flexibles; todas opcionales salvo un identificador):
    link          URL del producto (…/p). Si falta, se arma desde el ID producto / SKU.
    id_producto   "ID del producto" (productId VTEX)
    sku           "ID de SKU" (itemId VTEX) o RefId interno
    nombre        "Nombre del producto" (informativo)

Categorías:
    OK     link vivo, el SKU pertenece al producto y hay stock con precio.
    DUDA   SKU vacío / no existe / pertenece a otro producto, ID producto no
           coincide, o el link redirige a otro producto.
    ROTO   404 / no responde / no existe en catálogo  -> subcategoría "404"
           sin stock o sin precio                     -> subcategoría "SIN STOCK"
"""
import argparse
import csv
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

# Tiendas donde se busca cuando la fila no trae link (en este orden).
STORES = {
    "Plaza Vea": "https://www.plazavea.com.pe",
    "Makro": "https://www.makro.plazavea.com.pe",
}
UA = "Mozilla/5.0 (link-monitor)"
TIMEOUT = 20


# ---------------------------------------------------------------- HTTP
def http_get(url, retries=3):
    """GET con reintentos y backoff. Devuelve (status, final_url, body)."""
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.status, r.geturl(), r.read()
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(2 ** attempt * 2)
                continue
            return e.code, e.geturl() if hasattr(e, "geturl") else url, b""
        except Exception as e:  # timeout, DNS, reset
            last = e
            time.sleep(2 ** attempt)
    return None, url, str(last).encode()


def api(base, query):
    """Catálogo público VTEX. None = error de API; [] = no encontrado."""
    status, _, body = http_get(base + "/api/catalog_system/pub/products/search" + query)
    if status != 200:
        return None
    try:
        return json.loads(body)
    except ValueError:
        return None


# ---------------------------------------------------------------- helpers
def clean_id(raw):
    if isinstance(raw, float) and raw.is_integer():
        raw = int(raw)  # Excel guarda IDs como número (2.0174285E7)
    return re.sub(r"\.0$", "", str(raw if raw is not None else "").strip())


def slug_from(url):
    m = re.search(r"/([^/]+)/p/?$", urllib.parse.urlparse(url).path)
    return m.group(1) if m else None


def base_of(url):
    u = urllib.parse.urlparse(url)
    return f"{u.scheme}://{u.netloc}"


def store_name(base):
    return next((n for n, b in STORES.items() if b == base), urllib.parse.urlparse(base).netloc)


def item_ids(item):
    ids = {str(item.get("itemId", "")), str(item.get("ean") or "")}
    for ref in item.get("referenceId") or []:
        ids.add(str(ref.get("Value", "")))
    return {i for i in ids if i}


def stock_of(item):
    qty, avail, price = 0, False, None
    for s in item.get("sellers", []):
        co = s.get("commertialOffer", {})
        qty += co.get("AvailableQuantity", 0) or 0
        avail = avail or bool(co.get("IsAvailable"))
        if co.get("Price") and price is None:
            price = co.get("Price")
    return qty, avail and qty > 0, price


def sip_of(item):
    """Precio con Tarjeta Sip, replicando el cálculo de la web de Plaza Vea.

    La promoción viene en commertialOffer.PromotionTeasers con nombre "TARJETA OH…"
    (nombre interno heredado). Tipos soportados, igual que la web:
      PromotionalPriceTableItemsDiscount: precio sin descuento − valor / unitMultiplier
      MaximumUnitPriceDiscount:           valor / unitMultiplier (precio fijo)
      PercentualDiscount:                 precio online − %
    Solo cuenta si el precio Sip es menor al precio online.
    Devuelve (precio_regular, precio_online, precio_sip | None).
    """
    mult = float(item.get("unitMultiplier") or 1) or 1
    for s in item.get("sellers", []):
        co = s.get("commertialOffer", {})
        price = co.get("Price") or 0
        if not price:
            continue
        regular = co.get("ListPrice") or price
        base = co.get("PriceWithoutDiscount") or regular
        sip = None
        for t in co.get("PromotionTeasers") or []:
            name = t.get("Name") or ""
            if "TARJETA OH" not in name.upper() and "TARJETA SIP" not in name.upper():
                continue
            if "LURIN" in name.upper():
                continue  # promoción oculta de una tienda específica
            params = (t.get("Effects") or {}).get("Parameters") or []
            if not params:
                continue
            kind, value = params[0].get("Name"), None
            if kind == "PromotionalPriceTableItemsIds" and len(params) > 1:
                kind = params[1].get("Name")
            if kind == "PromotionalPriceTableItemsDiscount" and len(params) > 1:
                value = float(params[1]["Value"])
                sip = round(100 * (base - value / mult)) / 100
            elif kind == "MaximumUnitPriceDiscount":
                sip = round(float(params[0]["Value"]) / mult, 2)
            elif kind == "PercentualDiscount":
                sip = round(price - price * float(params[0]["Value"]) / 100, 2)
            if sip is not None:
                break
        if sip is not None and not (0 < sip < price):
            sip = None
        return regular, price, sip
    return None, None, None


def find_sku_owner(base, sku):
    """Producto al que pertenece un SKU (itemId VTEX, RefId o EAN)."""
    for fq in ("skuId", "alternateIds_RefId", "alternateIds_Ean"):
        res = api(base, f"?fq={fq}:{urllib.parse.quote(sku)}")
        if res:
            return res[0]
    return None


def is_404(status, final):
    return status >= 400 or "/Sistema/404" in final or "ProductLinkNotFound" in final


# ---------------------------------------------------------------- core
COLUMNS = ["tienda", "link", "id_producto", "sku", "refid", "nombre", "categoria", "subcategoria",
           "motivo", "producto_web", "id_producto_web", "refid_web", "sku_pertenece",
           "stock", "precio_regular", "precio", "tiene_sip", "precio_sip", "dscto_sip_soles",
           "dscto_sip_pct", "dscto_sip_vs_regular_pct", "http", "url_final", "producto_del_sku", "revisado"]


def check(row):
    link = (row.get("link") or "").strip()
    pid, sku, refid = clean_id(row.get("id_producto")), clean_id(row.get("sku")), clean_id(row.get("refid"))
    out = dict.fromkeys(COLUMNS, "")
    out.update(link=link, id_producto=pid, sku=sku, refid=refid, nombre=str(row.get("nombre") or "").strip(),
               revisado=time.strftime("%Y-%m-%d %H:%M:%S"))

    def done(cat, motivo, sub=""):
        out.update(categoria=cat, subcategoria=sub, motivo=motivo)
        return out

    p, redirected = None, False

    if link:
        # --- Camino A: la fila trae link
        if not link.startswith("http"):
            link = STORES["Plaza Vea"] + "/" + link.lstrip("/")
            out["link"] = link
        base = base_of(link)
        out["tienda"] = store_name(base)
        status, final, _ = http_get(link)
        out["http"], out["url_final"] = status, final
        if status is None:
            return done("ROTO", "No responde (timeout/conexión)", "404")
        if is_404(status, final):
            return done("ROTO", "Link va a 404 / producto no encontrado", "404")
        slug = slug_from(final) or slug_from(link)
        if not slug:
            return done("DUDA", "El link no es una página de producto (/…/p)")
        prod = api(base, f"/{urllib.parse.quote(slug)}/p")
        if prod is None:
            return done("DUDA", "Error consultando la API (reintentar)")
        if not prod:
            return done("ROTO", "Producto no existe en catálogo", "404")
        p = prod[0]
        redirected = bool(slug_from(link)) and slug_from(link) != slug
    else:
        # --- Camino B: sin link -> buscar por ID producto / SKU en cada tienda
        if not pid and not sku:
            return done("DUDA", "No se reconocieron columnas: revise que la fila tenga link, ID de producto o SKU")
        api_error = False
        for name, base in STORES.items():
            res = api(base, f"?fq=productId:{urllib.parse.quote(pid)}") if pid else []
            if res is None:
                api_error = True
                continue
            if res:
                p = res[0]
            elif sku:
                p = find_sku_owner(base, sku)
            if p:
                out["tienda"] = name
                break
        if not p:
            if api_error:
                return done("DUDA", "Error consultando la API (reintentar)")
            return done("ROTO", "No existe en Plaza Vea ni Makro", "404")
        link = p.get("link", "")
        out["link"] = link
        status, final, _ = http_get(link)
        out["http"], out["url_final"] = status, final
        if status is None:
            return done("ROTO", "No responde (timeout/conexión)", "404")
        if is_404(status, final):
            return done("ROTO", "La página del producto va a 404", "404")

    out["producto_web"] = p.get("productName", "")
    out["id_producto_web"] = p.get("productId", "")
    out["refid_web"] = p.get("productReference", "")

    # ¿El SKU pertenece al producto?
    match = None
    if sku:
        for it in p.get("items", []):
            if sku in item_ids(it) or sku == str(p.get("productReference")):
                match = it
                break
    out["sku_pertenece"] = "SI" if match else "NO"

    # Stock y precio (del SKU coincidente; si no, de todo el producto)
    stocks = [stock_of(i) for i in ([match] if match else p.get("items", []))]
    out["stock"] = sum(s[0] for s in stocks)
    out["precio"] = next((s[2] for s in stocks if s[2]), "")
    in_stock = any(s[1] for s in stocks)

    # Precio con Tarjeta Sip y su descuento (contra el precio online y el regular)
    regular, online, sip = sip_of(match or (p.get("items") or [{}])[0])
    out["precio_regular"] = regular or ""
    out["tiene_sip"] = "SI" if sip else "NO"
    if sip:
        out["precio_sip"] = sip
        out["dscto_sip_soles"] = round(online - sip, 2)
        out["dscto_sip_pct"] = round(100 * (online - sip) / online, 1)
        out["dscto_sip_vs_regular_pct"] = round(100 * (regular - sip) / regular, 1) if regular else ""

    # Clasificación (las dudas de identidad van antes que el stock)
    if not sku:
        return done("DUDA", "Fila sin SKU")
    if not match:
        owner = find_sku_owner(base_of(link), sku)
        if owner:
            out["producto_del_sku"] = f'{owner.get("productName")} ({owner.get("link")})'
            return done("DUDA", "El SKU pertenece a otro producto")
        return done("DUDA", "SKU no encontrado en catálogo")
    if pid and pid != str(p.get("productId")):
        return done("DUDA", "El ID de producto no corresponde al link/SKU")
    if refid and refid != sku and refid != str(p.get("productReference")):
        return done("DUDA", f"El código {refid} no corresponde (el producto tiene RefId {p.get('productReference')})")
    if redirected:
        return done("DUDA", "El link redirige a otro producto")
    if not in_stock or not out["precio"]:
        return done("ROTO", "Sin stock / sin precio", "SIN STOCK")
    return done("OK", "Link vivo, SKU pertenece, con stock")


def check_many(rows, workers=8):
    with ThreadPoolExecutor(workers) as ex:
        return list(ex.map(check, rows))


# ---------------------------------------------------------------- I/O
ALIASES = {
    "link": ("link", "url", "enlace", "links", "urls"),
    "id_producto": ("id_producto", "id del producto", "id producto", "productid", "product id"),
    "sku": ("sku", "skus", "id de sku", "id sku", "skuid"),
    "refid": ("refid", "ref id", "cod canasta", "cod producto", "codigo", "código", "cod"),
    "nombre": ("nombre", "nombre del producto", "producto", "descripcion", "descripción"),
}


def norm_row(raw):
    """Mapea encabezados libres. Acepta coincidencia exacta o por prefijo ("Descripción C")."""
    low = {re.sub(r"\s+", " ", str(k).strip().lower()): v for k, v in raw.items() if k is not None}
    out = {}
    for key, names in ALIASES.items():
        out[key] = ""
        for exact in (True, False):
            for a in names:
                for col, v in low.items():
                    if v in (None, "") or not (col == a if exact else col.startswith(a + " ")):
                        continue
                    out[key] = v
                    break
                if out[key] != "":
                    break
            if out[key] != "":
                break
    # Si solo vino un código interno, se usa como SKU (el validador acepta RefId)
    if out["sku"] in (None, "") and out["refid"] not in (None, ""):
        out["sku"] = out["refid"]
    return out


def read_input(path):
    if path.lower().endswith((".xlsx", ".xlsm")):
        import openpyxl
        ws = openpyxl.load_workbook(path, read_only=True, data_only=True).active
        rows = list(ws.iter_rows(values_only=True))
        head = [str(h or "") for h in rows[0]]
        return [norm_row(dict(zip(head, r))) for r in rows[1:] if any(r)]
    with open(path, newline="", encoding="utf-8-sig") as f:
        sample = f.read(4096); f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        return [norm_row(r) for r in csv.DictReader(f, dialect=dialect)]


def write_output(results, path):
    if path.lower().endswith(".csv"):
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=COLUMNS); w.writeheader(); w.writerows(results)
        return
    import openpyxl
    from openpyxl.styles import Font, PatternFill
    wb = openpyxl.Workbook()
    fills = {"OK": "C6EFCE", "DUDA": "FFEB9C", "ROTO": "FFC7CE"}
    cat_col = COLUMNS.index("categoria") + 1
    summary = wb.active; summary.title = "Resumen"
    summary.append(["Categoría", "Cantidad", "%"])
    n = len(results)
    pct = lambda c: round(100 * c / n, 1) if n else 0
    for cat in ("OK", "DUDA", "ROTO"):
        c = sum(r["categoria"] == cat for r in results); summary.append([cat, c, pct(c)])
    for sub in ("404", "SIN STOCK"):
        c = sum(r["subcategoria"] == sub for r in results); summary.append([f"  ROTO · {sub}", c, pct(c)])
    summary.append(["TOTAL", n, 100])
    for cat in ("TODOS", "OK", "DUDA", "ROTO"):
        ws = wb.create_sheet(cat)
        ws.append(COLUMNS)
        for c in ws[1]: c.font = Font(bold=True)
        for r in results:
            if cat == "TODOS" or r["categoria"] == cat:
                ws.append([r[k] for k in COLUMNS])
                ws.cell(ws.max_row, cat_col).fill = PatternFill("solid", fgColor=fills.get(r["categoria"], "FFFFFF"))
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
    wb.save(path)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("entrada")
    ap.add_argument("-o", "--salida", default="resultado_monitor.xlsx")
    ap.add_argument("-w", "--workers", type=int, default=8, help="peticiones en paralelo")
    a = ap.parse_args()

    rows = read_input(a.entrada)
    print(f"Revisando {len(rows)} filas con {a.workers} workers…", file=sys.stderr)
    t0 = time.time()
    results = check_many(rows, a.workers)
    write_output(results, a.salida)
    for cat in ("OK", "DUDA", "ROTO"):
        print(f"{cat:5} {sum(r['categoria'] == cat for r in results)}")
    print(f"Salida: {a.salida}  ({time.time()-t0:.0f}s)")


if __name__ == "__main__":
    main()
