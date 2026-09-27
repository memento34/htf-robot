# OKX gerçek fiyatlı yerel paper robotu

Bu proje OKX'in **üretim ortamındaki herkese açık** USDT teminatlı linear perpetual swap fiyatlarını okur. Yerel SQLite defterinde 10.000 USDT başlangıç bakiyesiyle sanal pozisyon açıp kapatır. OKX Demo Trading hesabı, API anahtarı veya borsaya emir gönderimi kullanılmaz.

## Railway'de açılmayan arayüzü düzeltme

Ekran görüntüsündeki `/usr/local/bin/python: No module named bot` hatası, Railway'in çalıştığı depo kökünde `bot/` paketini bulamadığını gösteriyor. Yeni ZIP **düz köklüdür**: açıldığında `main.py`, `bot/`, `Dockerfile` ve `railway.json` doğrudan görünür. ZIP'in *içeriğini* GitHub deponuzun köküne yükleyin. Eski depoda `okx-paper-bot/` alt klasörü kaldıysa onu kaldırıp içeriğini köke taşıyın veya Railway **Root Directory** değerini `okx-paper-bot` olarak ayarlayın.

1. Railway servisinizin GitHub deposunu güncelleyin. `main.py` ve `bot/` aynı depo kökünde olmalı.
2. Railway **Settings → Deploy → Custom Start Command** içinde eski `python -m bot ...` komutu varsa `python main.py` yapın veya özel komutu silin; yeni `railway.json` ve Dockerfile bu komutu kullanır.
3. **Variables** bölümünde `DASHBOARD_PASSWORD` için en az 12 karakterlik bir şifre belirleyin. Eski `OKX_DEMO_API_KEY`, `OKX_DEMO_SECRET_KEY`, `OKX_DEMO_PASSPHRASE` değişkenlerini kaldırabilirsiniz; yazılım bunları okumaz. Daha önce `BOT_DB` tanımladıysanız kaldırın; yeni sürüm `PAPER_DB` kullanır.
4. 30 günlük kaydın yeniden dağıtımlarda korunması için Railway Volume'u `/data` yoluna bağlayın. İsterseniz `PAPER_DB=/data/paper.sqlite3` ekleyin; Railway Volume yolu algılandığında bu konum varsayılandır. Servisi **tek replika** çalıştırın.
5. İlk açılış için `BOT_ENABLED=false` bırakın. Domain açılır, şifreli panel gelir ve OKX gerçek piyasa fiyatları okunur. Sonra `BOT_ENABLED=true` yapın; yerel paper işlemler ve 30 günlük ileri test başlar. `PAPER_START_BALANCE_USDT=10000` sadece **yeni** defter ilk oluşturulduğunda uygulanır. Var olan hesabın bakiyesi yeniden dağıtımda sıfırlanmaz.
6. Railway domaininde `/health` yolu 200 dönmelidir. Sunucu `0.0.0.0:$PORT` adresinde dinler. Panel yine açılmazsa yeni deploy logunda `main.py`/`bot` dosyalarının kökte bulunduğunu ve hangi Start Command'ın çalıştığını kontrol edin.

## Ne gösterir?

Panel sanal özsermaye, serbest bakiye, açık pozisyonlar, kararlar, paper açılış/kapanışları ve 30 günlük raporu gösterir. Panel salt okunurdur. Gerçek OKX fiyatları üretim `openapi.okx.com` herkese açık `public/` ve `market/` uçlarından gelir. Kod diğer endpoint ve HTTP yöntemlerini reddeder. API anahtarı gerekmez.

Robot saatlik olarak tüm uygun `*-USDT-SWAP` enstrümanlarını keşfeder. Varsayılan olarak 15 saniyede bir 40 paritelik döner grup analiz edilir. Açık pozisyonların sanal stop ve hedefleri yaklaşık 5 saniyede bir gerçek emir defteri bid/ask fiyatıyla kontrol edilir. İşlemler, o andaki bid/ask, varsayılan 5 bps komisyon ve 3 bps kayma ile hesaplanır; sözleşme adedi OKX lot adımına yuvarlanır. Bu varsayımlar gerçekleşmiş borsa fill'lerini garanti etmez. Ağ kesintisi sırasında stop uygulanamaz; bağlantı döndüğünde o anki fiyattan sanal kapanış olur. Fonlama, likidasyon, derinlik etkisi ve finansman maliyetleri uygulanmaz.

Strateji 1 dakikalık kapanmış mumları, EMA, RSI/Bollinger ve emir defteri dengesini kullanır. Haber kaynağı bağlı olmadığı için haber ajanı nötrdür. Her sembol için geçmiş veriyle WFA onayı gerekir; onaylanmayan sembolde karar `hold` kalır. Bu yüzden sık işlem talebi **işlem garantisi** değildir ve sistem gerçek anlamda düşük gecikmeli HFT değildir. Çok sayıda piyasayı sürekli tarayan, risk sınırları olan bir paper deneyidir. Başlangıçta WFA verisi indirilirken ilk sanal işlemler gecikebilir.

Risk sınırları varsayılan olarak en çok 3 açık pozisyon, toplam özsermayenin %30'u kadar nominal maruziyet, işlem başı %0,5 risk ve günlük %2 zarar sınırıdır. Borsa saati/veri kalitesi sorunları yeni girişleri durdurur. İlgili değişkenler `.env.example` dosyasındadır. 30 günlük süre bitince yeni girişler durur; açık paper pozisyonların sanal stop/hedef kontrolü sürer.

## Yerel çalışma

Python 3.11+ ile depo kökünde:

```powershell
$env:PAPER_DB = ".\paper.sqlite3"
$env:DASHBOARD_PASSWORD = "yerel-deneme-icin-uzun-sifre"
python -m unittest discover -s tests -v
python main.py
```

Panel `http://127.0.0.1:8080` adresindedir. `BOT_ENABLED=true` ayarlanmadıkça yeni pozisyon açılmaz. Araştırma komutları: `python main.py list-instruments`, `python main.py backtest BTC-USDT-SWAP`, `python main.py validate BTC-USDT-SWAP`, `python main.py report`, `python main.py audit-verify`. `backtest-all` tüm uygun semboller için geçmiş mumları indirir ve uzun sürebilir; geçmiş backtest ileri paper performansı değildir.

**Doğrulama sınırı:** Kodun yerel testleri gerçek ağ olmadan yapılır. Bu paket hazırlandığında kullanıcı Railway dağıtımı ve gerçek OKX veri akışı üzerinde 30 günlük performans henüz oluşmamıştır; panelde görülen rapor yalnızca dağıtımdan sonra biriken kayıtları gösterir.
