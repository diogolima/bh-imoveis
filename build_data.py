#!/usr/bin/env python3
"""Gera data.js (usado pelo index.html) a partir das planilhas de ITBI.

Geocodifica cada endereço usando o CNEFE do Censo 2022 (IBGE), que traz
latitude/longitude de todos os endereços de Belo Horizonte. O arquivo do IBGE
é baixado uma única vez para .cache/.

Uso: python3 build_data.py
"""
import csv
import glob
import io
import json
import os
import re
import statistics
import unicodedata
import urllib.request
import zipfile
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, ".cache")
CNEFE_URL = (
    "https://ftp.ibge.gov.br/Cadastro_Nacional_de_Enderecos_para_Fins_Estatisticos/"
    "Censo_Demografico_2022/Arquivos_CNEFE/CSV/Municipio/31_MG/3106200_BELO_HORIZONTE.zip"
)
CNEFE_CSV = os.path.join(CACHE, "3106200_BELO_HORIZONTE.csv")

# Limites aproximados do município, para descartar coordenadas absurdas
LAT_MIN, LAT_MAX, LON_MIN, LON_MAX = -20.07, -19.76, -44.07, -43.85


def norm(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9 ]", " ", s.upper())).strip()


def median_point(pts):
    return (statistics.median(p[0] for p in pts), statistics.median(p[1] for p in pts))


def ensure_cnefe():
    if os.path.exists(CNEFE_CSV):
        return
    os.makedirs(CACHE, exist_ok=True)
    print("Baixando CNEFE (IBGE)...")
    with urllib.request.urlopen(CNEFE_URL) as r:
        data = r.read()
    zipfile.ZipFile(io.BytesIO(data)).extractall(CACHE)


def load_cnefe():
    """Índices: (cep, numero), (rua, numero), cep -> [(numero, lat, lon)], rua -> idem."""
    by_cep_num = defaultdict(list)
    by_name_num = defaultdict(list)
    by_cep = defaultdict(list)
    by_name = defaultdict(list)
    with open(CNEFE_CSV, encoding="latin-1", newline="") as fh:
        for r in csv.DictReader(fh, delimiter=";"):
            try:
                lat, lon = float(r["LATITUDE"]), float(r["LONGITUDE"])
                num = int(r["NUM_ENDERECO"] or 0)
            except ValueError:
                continue
            if not (LAT_MIN < lat < LAT_MAX and LON_MIN < lon < LON_MAX):
                continue
            cep = r["CEP"].strip().zfill(8)
            name = norm(f'{r["NOM_TITULO_SEGLOGR"]} {r["NOM_SEGLOGR"]}')
            p = (lat, lon)
            by_cep[cep].append((num, lat, lon))
            by_name[name].append((num, lat, lon))
            if num:
                by_cep_num[(cep, num)].append(p)
                by_name_num[(name, num)].append(p)
    return by_cep_num, by_name_num, by_cep, by_name


def dist2(a, b):
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2


def nearest_number(cands, num, anchor=None):
    """Ponto do número mais próximo (preferindo mesmo lado da rua)."""
    cands = [c for c in cands if c[0]]
    if anchor:
        # mesma rua pode existir em outro bairro: fica só o que está a < ~1.5 km do CEP
        cands = [c for c in cands if dist2(c[1:], anchor) < 0.014**2]
    if not cands:
        return None
    same = [c for c in cands if c[0] % 2 == num % 2] or cands
    best = min(abs(c[0] - num) for c in same)
    if best > 150:
        return None
    return median_point([c[1:] for c in same if abs(c[0] - num) == best])


def geocode(name, num, cep, idx):
    """Retorna (lat, lon, precisao): 0 = endereço exato, 1 = aproximado, 2 = centro do CEP."""
    by_cep_num, by_name_num, by_cep, by_name = idx
    cep_pts = by_cep.get(cep)
    anchor = median_point([c[1:] for c in cep_pts]) if cep_pts else None
    if num:
        pts = by_cep_num.get((cep, num))
        if pts:
            return (*median_point(pts), 0)
        pts = by_name_num.get((name, num))
        if pts:
            if anchor:
                near = [p for p in pts if dist2(p, anchor) < 0.014**2]
                if near:
                    return (*median_point(near), 0)
            else:
                return (*median_point(pts), 0)
        if cep_pts:
            p = nearest_number(cep_pts, num)
            if p:
                return (*p, 1)
        p = nearest_number(by_name.get(name, []), num, anchor)
        if p:
            return (*p, 1)
    if anchor:
        return (*anchor, 2)
    pts = by_name.get(name)
    if pts and not num:
        return (*median_point([c[1:] for c in pts]), 2)
    return None


def parse_number(s):
    s = s.strip().replace("R$", "").strip()
    if not s or s == "-":
        return None
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif s.count(".") > 1 or re.fullmatch(r"\d{1,3}\.\d{3}", s):
        s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


# Colunas usadas, identificadas pelo início do nome no cabeçalho (os nomes variam um pouco entre meses)
COLUMNS = [
    "ENDERECO", "BAIRRO", "ANO DE CONSTRUCAO", "AREA CONSTRUIDA ADQUIRIDA",
    "TIPO CONSTRUTIVO", "DESCRICAO TIPO OCUPACAO", "VALOR DECLARADO", "DATA QUITACAO",
]
C_ADDR, C_BAIRRO, C_ANO, C_AREA, C_TIPO, C_OCUP, C_VALOR, C_DATA = range(8)


def read_text(path):
    raw = open(path, "rb").read()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def load_itbi():
    """Lê todos os .csv da pasta; cada linha vira uma lista na ordem de COLUMNS."""
    rows = []
    for f in sorted(glob.glob(os.path.join(HERE, "*.csv"))):
        rd = csv.reader(io.StringIO(read_text(f), newline=""), delimiter=";")
        header = [norm(c) for c in next(rd)]
        try:
            pos = [next(i for i, h in enumerate(header) if h.startswith(c)) for c in COLUMNS]
        except StopIteration:
            raise SystemExit(f"{os.path.basename(f)}: cabeçalho inesperado, colunas esperadas: {COLUMNS}")
        for r in rd:
            if len(r) > max(pos) and r[pos[C_ADDR]].strip():
                rows.append([r[i].strip() for i in pos])
    return rows


ADDR_RE = re.compile(r"^(\S+)\s+(.*?)(?:\s+(\d+)\s*[A-Z]?)?$")


def main():
    ensure_cnefe()
    rows = load_itbi()
    print(f"{len(rows)} transações lidas")
    idx = load_cnefe()

    locs = {}
    stats = Counter()
    for r in rows:
        parts = [p.strip() for p in r[0].split(" - ")]
        if len(parts) < 5:
            stats["endereco_invalido"] += 1
            continue
        base, compl, cep = parts[0], " - ".join(parts[1:-4]), parts[-3].replace("-", "")
        if compl == r[1]:  # bairro repetido no lugar do complemento
            compl = ""
        valor, area = parse_number(r[C_VALOR]), parse_number(r[C_AREA])
        d = r[C_DATA].split("/")
        if not valor or valor <= 0 or len(d) != 3:
            stats["sem_valor_ou_data"] += 1
            continue
        key = (base, cep)
        loc = locs.get(key)
        if loc is None:
            m = ADDR_RE.match(base)
            name, num = norm(m.group(2)), int(m.group(3) or 0)
            g = geocode(name, num, cep, idx)
            loc = locs[key] = {"g": g, "addr": base, "cep": parts[-3], "bairro": r[1], "t": []}
        if loc["g"] is None:
            stats["nao_localizadas"] += 1
            continue
        stats[f"precisao_{loc['g'][2]}"] += 1
        ano = int(r[C_ANO]) if r[C_ANO].isdigit() and int(r[C_ANO]) > 0 else 0
        loc["t"].append([int(d[2] + d[1] + d[0]), round(valor), round(area or 0, 2), ano, r[C_TIPO], r[C_OCUP], compl])

    tipos = sorted({t[4] for l in locs.values() for t in l["t"]})
    ocups = sorted({t[5] for l in locs.values() for t in l["t"]})
    out = []
    for l in locs.values():
        if not l["t"]:
            continue
        l["t"].sort(key=lambda t: -t[0])
        ts = [[t[0], t[1], t[2], t[3], tipos.index(t[4]), ocups.index(t[5]), t[6]] for t in l["t"]]
        out.append([round(l["g"][0], 6), round(l["g"][1], 6), l["g"][2], l["addr"], l["bairro"], l["cep"], ts])

    with open(os.path.join(HERE, "data.js"), "w", encoding="utf-8") as fh:
        fh.write("window.ITBI=")
        json.dump({"tipos": tipos, "ocups": ocups, "locs": out}, fh, ensure_ascii=False, separators=(",", ":"))
        fh.write(";\n")
    print(f"{len(out)} localizações, {sum(len(l[6]) for l in out)} transações no mapa")
    for k, v in sorted(stats.items()):
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
