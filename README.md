# PolyBOT

Bot Python untuk memindai prediction market Polymarket non-crypto dan menjalankan entry/exit dengan batas risiko. Mode default memakai **saldo simulasi $11**, tanpa private key, tanpa deposit, dan tanpa API AI berbayar. Python 3.11+ diperlukan.

**Belum ada bukti bahwa strategi ini menghasilkan profit atau memiliki win rate tertentu.** Target profit $0,50 dari entry $1 berarti sekitar +50% sebelum biaya. Target tinggi dan risiko rendah tidak selalu tersedia bersamaan. Bot boleh tidak trading sama sekali. Uji unit dan demo hanya menguji perilaku software, bukan keunggulan trading.

## Jalankan di PowerShell

Dashboard pemantauan lokal tersedia di folder `dashboard`, menggunakan Next.js dan Tailwind CSS:

```powershell
cd D:\PolyBOT\dashboard
npm run dev
```

Buka **http://127.0.0.1:3000**. Dashboard membaca akun dan ledger bot tanpa membuat order. Detail fitur, data, dan cara produksi: [dashboard/README.md](dashboard/README.md).

## Pantau dan kendalikan dari Telegram

Bot Telegram menyediakan ringkasan akun real/paper, posisi, aktivitas ledger bot, grafik ekuitas, serta kontrol `/paper_once`, `/paper_on`, `/paper_off`, `/live_on`, dan `/live_off`. Pairing membatasi akses ke satu akun pemilik. Layanan dapat berjalan tersembunyi di Windows; trading menunggu perintah dari HP setiap kali layanan dimulai. Panduan token, pairing, perintah, dan startup: [docs/TELEGRAM.md](docs/TELEGRAM.md).

Kirim `/scan` untuk memindai peluang non-crypto sekarang tanpa order, atau `/scan live` untuk menerapkan syarat forecast live. Scan memakai modal analisis dari konfigurasi, bukan saldo aktual akun, dan tidak mengubah ledger. `/scan_last paper` atau `/scan_last live` menampilkan laporan siklus bot yang tersimpan.

Penyaringan manual dapat diatur lewat [scanner.toml](scanner.toml), terpisah dari konfigurasi trading. Profil saat ini memperluas analisis hingga 120 outcome dan menampilkan daftar pantauan yang belum memenuhi syarat entry, tanpa menaikkan budget $1. Melonggarkan penyaringan bukan jaminan risiko lebih rendah atau adanya peluang yang layak.

Konfigurasi entry paper/live saat ini memakai batas bawah probabilitas dan confidence 70%, edge 5%, likuiditas minimum $200, serta volume harian $25. Nilai ini berlaku saat worker berikutnya dimulai. Ukuran entry $1, target profit minimum $0.50, dan batas kerugian tetap berlaku.

Bot dapat memilih **Yes maupun No**. Entry No dilakukan dengan `BUY` saham No untuk mengambil posisi pada hasil "tidak terjadi"; `SELL` digunakan saat keluar dari saham Yes atau No yang sudah dimiliki. Telegram menampilkan outcome pada entry dan exit. Forecast harus cocok dengan token outcome yang dipilih; forecast Yes tidak otomatis dibalik menjadi No. Batas budget dan risiko berlaku pada keduanya, dengan maksimal satu posisi per event. Penjelasan feed untuk No tersedia di [docs/FORECASTS.md](docs/FORECASTS.md#memilih-yes-atau-no).

Scanner mengelompokkan kedua outcome pasar agar Yes dan No diperiksa berdekatan dengan budget analisis yang sama. Jika batas waktu atau jumlah kandidat memotong pasangan, siklus worker berikutnya melanjutkan outcome yang belum diperiksa. Setiap outcome tetap memakai orderbook dan forecast miliknya; tidak ada kewajiban entry jika keduanya gagal syarat.

**Entry eksplorasi paper aktif** melalui `paper_entry_mode = "explore"`. Jika tidak ada forecast, paper dapat mencoba satu entry per siklus berdasarkan orderbook yang dapat dieksekusi, biaya, budget $1, dan batas portofolio. Entry ini diberi label `paper_exploratory`; probabilitas dan edge tidak diperkirakan. Target $0.50 adalah tujuan exit, bukan prediksi profit. Horizon eksplorasi maksimum 6 jam atau sebelum penutupan pasar, dan perkiraan rugi langsung akibat spread/biaya harus paling besar separuh batas stop-loss (maksimum 10%). Jalur ini tidak dapat dipakai oleh live. Ubah ke `paper_entry_mode = "forecast_required"` untuk mewajibkan forecast kembali pada paper.

```powershell
cd D:\PolyBOT
.\.venv\Scripts\python.exe -m polybot.telegram --pair
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\telegram.ps1 -Action Start
```

`/live_on` menjalankan trading uang asli otomatis sesuai strategi dan pemeriksaan risiko yang sama. Komputer perlu tetap menyala, tidak sleep, dan tersambung internet; dashboard web tidak perlu dijalankan untuk memakai Telegram.

## Bot dari terminal

Untuk menjalankan bot Python:

```powershell
cd D:\PolyBOT
python -m polybot demo
python -m polybot doctor
python -m polybot scan
python -m polybot run --once
python -m polybot run
```

`demo` sepenuhnya offline, menggunakan harga dan forecast buatan, serta ledger sementara. `scan` mengambil data publik dan menampilkan kandidat/alasan penolakan tanpa order. `run` mencatat transaksi simulasi berdasarkan depth orderbook nyata. Hentikan proses dengan Ctrl+C. Paper mode tidak memerlukan instalasi paket tambahan.

```powershell
python -m polybot status
python -m polybot stop
python -m polybot resume
```

`stop` membuat `data/STOP`: semua pengiriman order berhenti pada pemeriksaan berikutnya, termasuk exit. Order yang sedang dikirim tidak dapat ditarik kembali oleh file ini; posisi yang sudah terbuka tetap berisiko. `resume` menghapus STOP, tetapi tidak menghapus batas drawdown atau order yang belum jelas statusnya. Proses tidak otomatis dijalankan di background atau saat Windows menyala; terminal dan koneksi perlu tetap aktif.

## Penerapan aturan Anda

| Aturan | Implementasi default |
| --- | --- |
| Modal $11, entry $1 | Maksimum biaya entry termasuk cadangan fee $1; tidak menaikkan ukuran untuk mengejar minimum order |
| Semua kategori kecuali crypto | Membaca event/market aktif Gamma; judul, deskripsi, tag, dan metadata event disaring untuk crypto/aset digital terkait |
| Prioritas profit hari itu | Pasar dekat waktu berakhir diprioritaskan; sinyal intraday memerlukan bukti target harga tersentuh dalam horizon, bukan sekadar kemungkinan menang saat settlement |
| Peluang 1 minggu–1 bulan | Horizon maksimum 720 jam; maksimum satu posisi lama, total modal lama $2 |
| Profit minimal $0,50 / 50% | Target net setelah cadangan fee dan pembulatan tick; target $1 dapat dipilih lewat `minimum_profit = 1.0` |
| Jual ketika profit cukup | Evaluasi bid yang benar-benar dapat menampung seluruh posisi; jual FOK ketika laba net mencapai target |
| Saldo $30 lalu entry $5 | Hanya jika lower probability dan confidence minimal 90%, forecast eksternal mengklaim terkalibrasi, serta semua batas portofolio masih terpenuhi |

Scan dibatasi `scan_pages` (default 5 halaman event), `max_candidates` (maksimum 40 outcome), dan `max_scan_seconds` (60 detik analisis, di luar discovery dan request yang masih berlangsung). Jendela kandidat bergiliran berdasarkan jumlah yang benar-benar diperiksa agar tidak selalu membaca outcome yang sama. Ini bukan jaminan cakupan seluruh pasar Polymarket. Filter crypto menggunakan metadata/istilah; klasifikasi yang salah atau istilah baru masih mungkin lolos. Perluas daftar di `polybot/strategy.py` jika ditemukan.

Minimum order berlaku per pasar dan dibaca dari bursa. Misalnya, minimum 5 shares pada harga $0,50 membutuhkan paling sedikit $2,50 sebelum fee; bot akan menolak entry tersebut dengan budget $1. Harga outcome $0,90 juga hanya memberi upside settlement sekitar 11%, sehingga tidak dapat memenuhi target +50%.

## Analisis dan sumber peluang

Ada dua jalur analisis:

1. **Riwayat harga gratis, untuk paper mode.** Model mencari pemulihan harga, memisahkan data training/holdout menurut waktu, memakai observasi yang tidak bertumpang tindih, dan melaporkan batas bawah statistik target-touch. Model tidak membaca berita atau mengetahui hasil politik/olahraga. Output selalu eksperimental, bukan probabilitas fundamental terkalibrasi. Syarat data dan target +50% sangat ketat; tidak menemukan kandidat adalah hasil yang valid.
2. **Forecast eksternal untuk live.** File `forecasts.json` memuat estimasi per token dengan waktu observasi, expiry, horizon, sumber, alasan, confidence, dan lower probability. File dibaca ulang otomatis tanpa restart. Data yang rusak, kedaluwarsa, ID tidak cocok, atau tanpa kalibrasi tidak dipakai untuk entry live. **Produsen forecast otomatis lintas sektor tidak disertakan**; integrasikan data/model yang telah diuji melalui format ini sebelum live. Jangan mengarang angka peluang hanya supaya order lolos.

Format, contoh, dan batas interpretasinya tersedia di [docs/FORECASTS.md](docs/FORECASTS.md). `calibrated=true` adalah pernyataan pembuat forecast, bukan verifikasi kalibrasi oleh bot. Untuk intraday, `probability_kind` wajib `target_touch`. `resolution` hanya dipakai pada horizon lebih dari 24 jam; estimasi settlement tidak menjamin target harga sebelum deadline.

`minimum_edge = 0.08` berarti estimasi expected net return minimal 8% terhadap biaya posisi, setelah memakai lower probability. Untuk target-touch, perhitungan memakai kerugian penuh jika target gagal, bukan mengasumsikan stop-loss pasti berhasil. Probabilitas menyentuh target tetap tidak membuktikan harga dapat dieksekusi sebelum stop-loss; ini bukan backtest kebijakan exit lengkap.

## Batas risiko

Nilai di [config.toml](config.toml) dapat disesuaikan:

- Cadangan cash $2, maksimum 3 posisi, maksimum 40% equity terpakai, satu posisi maksimum 18% equity.
- Satu event hanya boleh memiliki satu posisi. Korelasi antar-event berbeda belum dimodelkan.
- Hentikan entry baru saat equity turun $1 dari baseline awal hari UTC atau drawdown 25% dari puncak. Exit tetap dikelola selama data tersedia. Drawdown latch tetap tersimpan saat restart.
- Stop-loss mencoba menjual saat nilai exit net turun 20%. Kerugian bisa lebih besar akibat gap, orderbook kosong, perubahan harga, atau koneksi terputus; seluruh biaya entry bisa hilang.
- Time exit pada akhir horizon forecast/expiry atau setidaknya 5 menit sebelum waktu akhir pasar yang diketahui. Tidak ada jaminan keluar sebelum penutupan mendadak. Redemption settlement otomatis belum tersedia; posisi yang sudah resolved perlu ditangani lewat Polymarket.
- Tidak ada martingale, leverage, averaging down, auto-deposit, atau penarikan ke rekening/wallet luar.

Valuasi equity menggunakan bid dengan depth, bukan last price atau midpoint. Quote posisi yang hilang menghentikan entry baru. Paper fill juga tetap simulasi: antrean, latensi, dan perubahan orderbook nyata dapat membuat hasil live berbeda.

## Menyiapkan live

Live **tidak diaktifkan** selama pembuatan proyek ini. Gunakan akun/wallet khusus bot; perdagangan manual atau bot lain mengganggu rekonsiliasi saldo. Pastikan tipe wallet didukung SDK, wallet sudah dibuat di Polymarket, jaringan/wilayah diizinkan, saldo collateral cukup, dan allowance disiapkan melalui alur resmi. Bot tidak melakukan deployment wallet atau approval token otomatis.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[live]"
Copy-Item .env.example .env
```

Isi `.env` **di komputer Anda sendiri**, jangan kirim private key/seed phrase ke chat. `POLYMARKET_WALLET_ADDRESS` adalah wallet Polymarket pemilik saldo; tidak selalu sama dengan alamat signer. Login email/Google memerlukan penyiapan signer/wallet sesuai dokumentasi resmi. Bot tidak mengekstrak kredensial browser. Dana trading memakai collateral yang digunakan Polymarket saat ini; baca [catatan API](docs/API_NOTES.md).

Sediakan `forecasts.json` dari produsen forecast independen yang layak. File contoh kosong sengaja tidak berisi rekomendasi palsu.

```powershell
.\.venv\Scripts\python.exe -m polybot doctor --live
.\.venv\Scripts\python.exe -m polybot run --live --once
.\.venv\Scripts\python.exe -m polybot run --live
.\.venv\Scripts\python.exe -m polybot status --live
```

`run --live` mengirim order uang asli tanpa konfirmasi per order ketika seluruh syarat terpenuhi. `doctor --live` membaca autentikasi/akun dan tidak membuat order/approval; autentikasi SDK dapat membuat/mengambil API credentials. Koneksi diblokir jika geoblock tidak dapat diverifikasi. Tidak ada bypass pembatasan wilayah.

SDK resmi dipin ke `polymarket-client==0.12.0`; SDK `py-clob-client` lama sudah dihentikan. Detail kontrak API, asumsi, dan sumber resmi ada di [docs/API_NOTES.md](docs/API_NOTES.md). Integrasi uang asli masih memerlukan pengujian dengan wallet Anda; unit test memakai exchange palsu dan tidak membuktikan autentikasi, allowance, atau settlement akun nyata.

## Penyimpanan dan pemulihan

Ledger paper/live terpisah di `data/paper.sqlite3` dan `data/live.sqlite3`. Ada process lock, jurnal intent sebelum submit, serta pencatatan posisi dan fill dalam transaksi SQLite. Jangan menghapus ledger live saat ada posisi/order.

Order FOK dikirim satu kali. Respons `matched` belum dihitung sebagai fill; adapter menunggu catatan settlement `CONFIRMED` dan perubahan saldo yang sesuai. Order dengan ID diketahui direkonsiliasi otomatis pada siklus berikutnya, termasuk sesudah restart. Error POST tanpa ID bisa berarti order sudah terkirim: bot menghentikan pengiriman dan tidak mencoba ulang.

```powershell
.\.venv\Scripts\python.exe -m polybot reconcile --live
```

`reconcile` hanya membaca status order yang ID-nya diketahui. Untuk `unknown` tanpa ID, periksa order/trade/balance akun melalui Polymarket lalu lakukan rekonsiliasi ledger dengan bukti transaksi; tidak ada tombol yang menandai order gagal secara sepihak. Deposit atau transaksi di luar bot akan menyebabkan perbedaan saldo dan menghentikan live. Penanganan manual juga diperlukan untuk sisa shares di bawah minimum jual.

## Verifikasi

```powershell
python -m unittest discover -v
python -m polybot demo
```

Test mencakup budget kecil, minimum size, fee, target profit, batas risiko, kategori crypto, data tidak valid, forecast kedaluwarsa, idempotensi ledger, order pending, dan restart. Tidak ada order uang asli dalam test.
