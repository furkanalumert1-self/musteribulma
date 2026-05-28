# Müşteri Bulma Ajanı

Türkiye odaklı B2B müşteri bulma ajanı. **Claude Code** içinde skill olarak çalışır, **Apify Google Maps** üzerinden hedef firmaları tarar, her birini puanlar, sıralı bir potansiyel müşteri listesi üretir.

> Sektör + konum ver → ajan 50 firma çeker → analiz eder → ranked liste + neden notları çıkarır.

---

## Özellikler

- **CLI-tabanlı.** Tek Python dosyası. Ek backend yok.
- **Claude Code skill** ile orkestre. `/musteri-bulma-ajani` veya doğal dille çağır.
- **Hard 50 firma / tarama** limiti — Apify maliyet kontrolü (~$0.30 / run).
- **Çıktı**: JSON · Markdown · CSV · lokal HTML dashboard.
- **Lokal landing sayfası** — sistemi göstermek için.
- Türkçe iletişim, Türkçe analiz, Türkçe çıktı.

## Gereksinimler

- Python 3.10+
- [Claude Code CLI](https://claude.com/claude-code) (skill'in çalışması için)
- [Apify](https://apify.com) hesabı + API token (ücretsiz hesapla $5 kredi gelir, ~15 tarama yeter)

## Hızlı kurulum

```bash
# 1) Projeyi klonla / indir
git clone <repo-url> musteri-bulma-ajani
cd musteri-bulma-ajani

# 2) Apify token'ını ekle
cp .env.example .env
# .env'yi aç, APIFY_API_TOKEN=... satırını kendi token'ınla doldur
# Token'ı buradan al: apify.com → Settings → Integrations → API tokens

# 3) Python ortamı + bağımlılıklar
python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt

# 4) Doğrula
.venv/bin/python musteri_ajan.py check
```

## Kullanım

### Claude Code içinde (önerilen)

Proje dizininde Claude Code'u aç:

```bash
cd musteri-bulma-ajani
claude
```

Sonra ajanı çağır — herhangi biri çalışır:

```
/musteri-bulma-ajani
```

```
İstanbul'da diş hekimi için müşteri bul
```

```
Ankara'da küçük inşaat firmaları için 50 lead çıkar
```

Skill seni adım adım yönlendirir: kriter alır → tarar → analiz eder → ranked liste verir.

### Doğrudan CLI

```bash
# Tarama (her zaman max 50)
.venv/bin/python musteri_ajan.py scrape \
  --query "diş hekimi" \
  --location "Kadıköy, İstanbul, Türkiye" \
  --max 50 \
  --output leads_raw.json

# Sıralı listeyi terminalde göster
.venv/bin/python musteri_ajan.py display leads_ranked.json

# CSV export
.venv/bin/python musteri_ajan.py export-csv leads_ranked.json --output leads.csv

# Lokal landing + dashboard
.venv/bin/python musteri_ajan.py serve
# → http://127.0.0.1:8765 (landing)
# → http://127.0.0.1:8765/dashboard.html (data viewer)
```

> CLI tek başına ham tarama yapar; **analiz ve "neden uygun" notları Claude tarafından** üretilir. Bu yüzden ideal kullanım Claude Code skill'i ile.

## Çıktı formatı

Her firma için:

```json
{
  "name": "Firma Adı",
  "category": "Diş Hekimi",
  "website": "https://...",
  "emails": ["info@..."],
  "phones": ["+90..."],
  "address": "...",
  "rating": 4.7,
  "reviews_count": 120,
  "sektor_uyumu": 9,
  "erisilebilirlik": 8,
  "sinyal": 8,
  "toplam_skor": 8.5,
  "not": "Yüksek puanlı klinik, email + website açık — outreach kolay."
}
```

Puanlama formülü:

```
toplam = sektor_uyumu*0.5 + erisilebilirlik*0.3 + sinyal*0.2
```

## Dosya yapısı

```
musteri-bulma-ajani/
├── musteri_ajan.py             # Python CLI
├── requirements.txt
├── .env.example                # Apify token template
├── .gitignore
├── index.html                  # Landing sayfası
├── dashboard.html              # Lokal data viewer
├── README.md
├── LICENSE
└── .claude/
    └── skills/
        └── musteri-bulma-ajani/
            └── SKILL.md        # Claude Code orchestration
```

## Limit ve maliyet

- **50 firma / tarama** — hard cap. Üç katmanda zorlanır: CLI argümanı, Apify input, dataset iterator.
- **Apify Google Maps Scraper** kullanır (`compass/crawler-google-places`).
- Ortalama maliyet: **~$0.15–0.30 / tarama**. Apify free tier ($5) ile ~15–30 tarama.

Limiti aşmak istemezsin — script clamp'ler. Daha fazla firma için ayrı sektör/konum ile ikinci bir tarama yap.

## Landing sayfasını kendine göre uyarla

`index.html` içinde footer ve hero bölümlerini düzenleyebilirsin:

- Operatör adı: `<div class="name">Burhan Kocabıyık</div>`
- Twitter: `<a href="https://twitter.com/burhankocabiyik">`
- Hero başlık/metni: hero section'da

## Lisans

[MIT](LICENSE). Özgürce kullan, fork'la, kendine göre uyarla.

## Katkı

Geri bildirim ve PR'lara açık. Issue açabilir veya doğrudan PR gönderebilirsin.

---

**Yapımcı**: [Burhan Kocabıyık](https://twitter.com/burhankocabiyik) · Arspar · 2026
