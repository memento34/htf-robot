# OKX Demo USDT Perpetual Paper Bot

Bu proje, OKX'in **USDT teminatlı, linear, `live` durumundaki perpetual swap** enstrümanlarını API'den keşfeder; 1 dakikalık kapanmış mumları tarar ve yalnızca OKX **Demo Trading** hesabına emir gönderir. Gerçek para API modu yoktur. Depo bir Railway worker olarak çalışır.

## Durum ve önemli sınırlar

- Kaynak kod ve ağsız güvenlik testleri hazırdır. Bu paket hazırlanırken OKX'e yerel ağ bağlantısı ve size ait demo anahtarları yoktu; **gerçek demo emirleri ve 30 günlük performans henüz test edilmedi**.
- Bu sistem kurumsal HFT değildir. Varsayılan olarak 60 saniyede bir 40 enstrümanlık döner bir grup tarar. Çok sayıda enstrüman keşfedilir, fakat hepsi aynı saniyede analiz edilmez; düşük 24 saatlik hacimliler risk filtresiyle elenir.
- Ayrı hızlı koruma döngüsü 5 saniyede bir borsa saati, demo equity ve açık pozisyonların likidasyon mesafesini okur. WebSocket/tick düzeyi mikro yapı HFT'si bu sürümde yoktur; bağlı TP/SL emirleri borsa tarafında çalışır.
- Haber/LLM ajanı, doğrulanmış haber sağlayıcısı olmadığı için nötr ve `confidence=0` üretir. Tarihsel order book olmadığı için order-flow stratejisinin WFA ağırlığı `0` kalır. Bu ikisi işlem başlatamaz. Bu eksikler sessizce uydurulmaz.
- 30 günlük backtest ile 30 günlük **ileri yönlü** demo performansı ayrı raporlanır. Backtest, geçmiş order book ve fonlamayı içermez. Backtestte ücret ve slipaj varsayımı uygulanır.
- Otomatik 90 günlük WFA yalnızca geçmişi yeterli enstrümanları onaylar. Onay yoksa karar `hold` olur. Sabit parametre doğrulamasıdır; parametre optimizasyonu içermez.
- Canlı koşulda sunucu tarafında TP/SL ana emre eklenir. Ana emir kısmi dolarsa veya attached algo başarısız olursa manuel inceleme gerekebilir. Bu nedenle başlangıçta tek küçük demo enstrümanı ile doğrulama önerilir.

## Mimari

| Katman | Uygulama |
| --- | --- |
| 1 Veri | OKX instruments, tickers, 1m candles, 5 seviye book; stale/gap kontrolü |
| 2 Haber | Doğrulanmış akış olmadığı için nötr |
| 3 Strateji | Bağımsız trend, mean reversion, order-flow ve volatilite fonksiyonları |
| 4 Backtest | Geçmiş 30 gün, işlem başına ücret/slipaj ve next-bar fill |
| 5 Walk-forward | 60 gün IS + 15 gün OOS, 15 gün kaydırmalı iki pencere |
| 6 Risk | 3x tavan, işlem başı %0,5, günlük %2 zarar, toplam %30 maruziyet, 3 pozisyon |
| 7 Emir | Demo header sabit; `clOrdId`, tekrar göndermeden uzlaştırma, attached TP/SL; 5 saniyelik koruma döngüsü |
| 8 Son karar | Tek deterministik oy/filtre noktası; risk veto'su değiştirilemez |
| 9 İzleme | SQLite hash zincirli audit, equity ve kapalı pozisyon geçmişi |

## OKX demo hesabı

1. Kendi OKX hesabınızda **Trade > Demo Trading** bölümüne geçin. Bu oturum ve API anahtarı kullanıcının OKX hesabında açılmalıdır; proje sizin adınıza hesap oluşturamaz.
2. Demo Trading içindeki **Personal Center > Demo Trading API > Create Demo Trading API Key** ile **yalnızca demo** API anahtarı oluşturun. En az Read ve Trade izinleri gerekir; para çekme izni vermeyin.
3. OKX hesap modunu Futures/Swap işlemlerine uygun şekilde ayarlayın. Varsayılan bot `net` pozisyon modunu bekler. Demo hesabınız `long/short` ise `OKX_POSITION_MODE=long_short` kullanın.
4. Demo hesabında USDT sanal varlığı ve işlem yetkisi bulunduğunu kontrol edin. Anahtarları asla GitHub'a veya sohbete eklemeyin.

OKX'in [demo API yönergesi](https://www.okx.com/docs-v5/en/) `x-simulated-trading: 1` başlığını ve demo anahtarını ister. Kod bu başlığı sabit gönderir; gerçek işlem moduna geçiş parametresi sunmaz.

## Railway kurulumu

1. Bu klasörü kendi özel GitHub deponuza aktarın. `.env` dosyasını ve gerçek anahtarları yüklemeyin.
2. Railway'de depodan bir servis oluşturun. `Dockerfile` otomatik kullanılır; worker için HTTP portu gerekmez.
3. Servise **kalıcı volume** ekleyip `/data` konumuna bağlayın. `BOT_DB=/data/bot.sqlite3` varsayılandır. Volume yoksa `BOT_ENABLED=true` ile açılış durur. Tek replika çalıştırın; birden fazla replika aynı sinyali işleyebilir.
4. Railway Variables bölümüne `.env.example` içindeki değişkenleri girin. `OKX_DEMO_API_KEY`, `OKX_DEMO_SECRET_KEY`, `OKX_DEMO_PASSPHRASE` yalnızca burada saklanmalıdır. Başlangıçta `BOT_ENABLED=false` bırakın.
5. İlk dağıtımda loglarda enstrüman keşfi, bakiye, saat farkı ve WFA sonuçlarını kontrol edin. Tarihsel WFA her aktif aday için 90 günlük 1m veri çeker; ilk onaylar zaman alabilir ve OKX rate limitleri nedeniyle maliyet yaratabilir.
6. Demo modunda hesap/stop/pozisyon kontrolleri doğrulandıktan sonra `BOT_ENABLED=true` yapın. Bu an 30 günlük sayaç başlar. Süre dolunca yeni girişler otomatik durur ve `PAPER_TEST_COMPLETE` raporu loga yazılır. Mevcut pozisyonlar için OKX'teki attached TP/SL emirlerini kontrol edin.

Yerel komutlar (`BOT_DB` için yazılabilir bir konum ayarlayın):

```powershell
$env:BOT_DB = ".\work\bot.sqlite3"
python -m unittest discover -s tests -v
python -m bot list-instruments
python -m bot backtest BTC-USDT-SWAP
python -m bot backtest-all --output .\reports\backtests-30d.jsonl
python -m bot validate BTC-USDT-SWAP
python -m bot report
python -m bot audit-verify
```

`backtest-all` dinamik bulunan tüm uygun perpetual çiftlerde 30 günlük 1m veriyi indirir; yüzlerce çiftte saatler sürebilir. Çıktı JSONL satır satır yazılır, tekrar çalıştırılınca tamamlanan semboller atlanır. Yerel ağ erişimi yoksa komutlar çalışmaz; Railway servisinde ağ erişimi gerekir.

## İşlem mantığı ve güvenlik

- Enstrüman listesi `GET /api/v5/public/instruments?instType=SWAP` üzerinden saatlik yenilenir. Yalnız `settleCcy=USDT`, `ctType=linear`, `state=live`, `*-USDT-SWAP` kabul edilir.
- 1m mum, trend (EMA9/21 ve 5m eğim), Bollinger/RSI dönüşü, order-book imbalance ve ATR rejimi değerlendirilir. WFA'sı reddedilen strateji ağırlığı `0` olur.
- Risk veto'su; stale/gap veri, saat farkı >2 saniye, günlük zarar, açık emir, pozisyon sınırı, bilinmeyen maruziyet, toplam maruziyet, 3 ardışık kapanmış zarar, API hata limiti, eksik pozisyon geçmişi ve çözümlenemeyen emir için yeni girişleri engeller.
- Ana emre borsa tarafında market fiyatlı stop ve take profit eklenir. Emirden önce pozisyon/açık emir tekrar okunur. Aynı sinyal veritabanında tekilleştirilir. POST belirsiz sonuç verirse bot **ikinci piyasa emrini göndermez**; `clOrdId` ile uzlaştırır ve çözülemezse yeni girişleri durdurur.
- Denetim kaydı append-only SQLite trigger ve SHA-256 zinciriyle korunur. Bu, veritabanı yöneticisine karşı kriptografik kanıt sağlamaz; volume ve yedekler yine sizin kontrolünüzdedir.
- Rapor, borsadan alınan gerçek demo equity gözlemlerini ve kapalı pozisyon geçmişini kullanır. İşlem başı R multiple için güvenilir risk atfı yoksa `null` gösterilir.

## Doğrulama durumu

Paket hazırlanırken `python -m unittest discover -s tests -v` ile 7 ağsız test geçti: demo header, risk veto, günlük kayıp/maruziyet, emir miktarı/stop, tam tarama döngüsünde WFA onayı yokken `hold`, hızlı döngüde likidasyon yakınlığı, audit zinciri ve idempotency. Gerçek OKX Demo API ve Railway üzerinde entegrasyon testi, size ait demo oturumu ve Railway kurulumu olmadan doğrulanamadı.
