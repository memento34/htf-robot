# Astra Paper — anahtarsız sanal perpetual izleme

AstraQuant fikrinden yola çıkan **ayrı, küçük bir paper uygulaması**. Orijinal AstraQuant'ın LLM, OKX hesap, demo ve emir kodunu içermez. Bu uygulamada borsa hesabı açılmaz, demo anahtarı istenmez ve gerçek emir gönderme yolu yoktur. İşlemler SQLite defterinde sanal olarak oluşur.

## Ne gösterir?

- BTC ve ETH USDT perpetual sözleşmelerinin açık OKX piyasa fiyatları.
- Her tamamlanmış 15 dakikalık mum için trend kararı, ortalamalar, ATR ve risk kontrolü.
- Sanal giriş ve çıkış dolumları; varsayımsal komisyon ve kayma; nakit, açık ve kapanmış kâr/zarar.
- Stop, hedef, günlük kayıp ve tepe düşüş sınırları; veri yoksa veya bayatsa işlem engeli.

Sinyal kuralı: son 12 ve 36 mumun kapanış ortalamaları karşılaştırılır. Güçlü yükselen trend ve kapanışın hızlı ortalamanın üzerinde olması `LONG`; tersinde `SHORT`; aksi halde `FLAT`. İşlem büyüklüğü hesap özkaynağının en çok %1'ini stop mesafesinde riske atacak ve %20 teminat × 2 kaldıraç sınırını aşmayacak şekilde hesaplanır. Sanal stop mesafesi `max(1.5 × ATR14, fiyatın %0.4'ü)`; hedef mesafenin iki katıdır. Günlük kayıp %3 ve tepeye göre düşüş %10 olursa yeni pozisyon açılmaz. Pozisyon açıkken stop/hedef için fiyat yaklaşık 30 saniyede bir kontrol edilir; yeni yön kararı sadece kapanmış 15 dakikalık mumda alınır.

Bu hesap **borsa simülasyonu değildir**. Emir defteri, gerçek dolum olasılığı, fonlama, likidasyon, spread değişimi ve vergi modellenmez. Sanal dolum son görülen fiyata sabit %0.02 kayma ve %0.05 komisyon uygular. Piyasa verisi ulaşmazsa fiyat uydurulmaz ve işlem açılmaz.

## Railway kurulumu

1. Bu klasörü kendi GitHub deponuza gönderin ve Railway'de **Deploy from GitHub repo** seçin. Projedeki `Dockerfile` tek bir web servisi oluşturur; Docker Compose gerekmez.
2. Web servisine bir **Railway Volume** bağlayın ve mount path olarak `/data` girin. Bu, sanal işlem kayıtlarının yeniden dağıtımda korunması için gereklidir. Railway volume'u root sahibiyle bağladığı için imaj da root olarak çalışır.
3. Servis için public domain oluşturun. Uygulama Railway'in `PORT` değişkenini otomatik okur. İsteğe bağlı `PAPER_SYMBOLS=BTC-USDT-SWAP,ETH-USDT-SWAP` ve `PAPER_START_CASH=10000` ayarlanabilir; API anahtarı gerekmez.
4. `/health` sağlıklı görünmeli. Ana sayfa işlem kontrol panelidir. İlk piyasa sorgusu ve ilk kapanmış mumdan sonra karar kayıtları görünür.

**Not:** OKX'in açık piyasa veri uçları internete yapılan anahtarsız HTTPS istekleridir. “Hiçbir API” ile internetten fiyat da alınmayacaksa, gerçek piyasa davranışı izlenemez; bu uygulama o durumda veri hatası gösterip işlemi durdurur.

## Yerel çalıştırma

Python 3.11 veya üstüyle `python app.py`; tarayıcıda `http://127.0.0.1:8080`. Standart kütüphane dışında Python paketi gerektirmez. `python -m unittest discover -s tests -v` ile ağsız testleri çalıştırın.

## Operasyon sınırları

- **Tek Railway replika** kullanın. Web sunucusu ve strateji zamanlayıcısı aynı süreçtedir; birden fazla replika aynı sanal hesabı eşzamanlı işletemez.
- Bu sürümde kullanıcı girişi yoktur. Public domaini bilen herkes paneldeki sanal sonuçları görebilir. Hassas veri barındırmayın.
- `PAPER_START_CASH` yalnızca ilk, boş defter oluşturulurken uygulanır. Yeniden başlatma sanal hesabı sıfırlamaz.
- Gerçek emir gönderen veya OKX özel hesap uçlarına bağlanan bir mod yoktur.

Kaynak fikri: [AstraQuant](https://github.com/0xethanq/astra-quant-agent). Bu yeniden uygulama, orijinal deponun hazır stratejisi ya da sonuçlarının aynısı olduğunu iddia etmez.
