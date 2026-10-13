# PolyBOT di Telegram

Telegram dapat menampilkan akun real dan simulasi, serta menjalankan dan menghentikan trading paper/live dari HP. Komputer tetap menjalankan Python; HP hanya menjadi pengendali. Website Next.js, Tailscale, port publik, dan webhook tidak diperlukan.

## Hubungkan akun Telegram Anda

1. Buat bot pribadi melalui [@BotFather](https://t.me/BotFather), gunakan `/newbot`, lalu simpan token yang diberikan pada `TELEGRAM_BOT_TOKEN` di `.env` lokal. Jika token sudah diisi, lewati langkah ini. Token adalah kredensial bot; jangan kirim ke chat atau taruh di URL browser. Lihat [panduan resmi Telegram](https://core.telegram.org/bots/tutorial).
2. Pasang dependensi dan periksa koneksi di PowerShell:

   ```powershell
   cd D:\PolyBOT
   .\.venv\Scripts\python.exe -m pip install -e ".[live,telegram]"
   .\.venv\Scripts\python.exe -m polybot.telegram --check
   ```

   `connected: true` berarti token dapat mengakses Telegram. `paired: false` berarti akun pemilik belum dihubungkan. Pemeriksaan ini tidak menjalankan trading.

3. Hubungkan satu akun pemilik:

   ```powershell
   .\.venv\Scripts\python.exe -m polybot.telegram --pair
   ```

   Buka `pair_url` yang muncul dengan Telegram di HP, lalu tekan **Start**. Tautan sekali pakai berlaku lima menit; jangan bagikan tautan tersebut. Anda juga dapat mengirim perintah `/pair ...` yang ditampilkan ke bot itu. Program menyimpan `TELEGRAM_ALLOWED_USER_ID` otomatis ke `.env` tanpa mengubah kredensial lain. Tidak perlu bot pihak ketiga untuk mencari user ID. Hentikan layanan Telegram yang sedang aktif sebelum pairing ulang.

4. Jalankan layanan tersembunyi:

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\telegram.ps1 -Action Start
   ```

   Tunggu pesan **PolyBOT tersambung** di Telegram. Jalankan `/help` untuk membuka menu. Menjalankan layanan belum menjalankan paper maupun live; trading dimulai hanya melalui perintah pemilik. Token dan private key tetap dimuat dari komputer, tidak dikirim sebagai pesan Telegram.

## Perintah di HP

Alternatif untuk pairing sekaligus menjalankan layanan di background:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\telegram.ps1 -Action Setup
Get-Content .\data\telegram.stdout.log -Tail 5
```

Buka `pair_url` dari log atau kirim perintah `/pair ...` yang tercantum. Kode untuk alur background ini berlaku 24 jam dan hanya dapat dipakai sekali. Kode yang belum kedaluwarsa dipertahankan saat restart, sehingga pesan yang sudah antre tetap bisa diproses. Sesudah pairing berhasil layanan langsung aktif; paper dan live tetap menunggu perintah. Kode pairing di log dan `data/telegram-pairing.json` bersifat pribadi; jangan dibagikan. Hanya jalankan Setup ketika layanan belum aktif. Untuk foreground, padanannya adalah `python -m polybot.telegram --setup --pair-timeout 86400`.

Perintah dashboard menerima mode `paper` atau `live`; `real` adalah alias `live`. Tanpa mode, bot memakai `paper`.

| Perintah | Hasil |
| --- | --- |
| `/help` | Menu dan daftar perintah |
| `/status` | Proses paper/live yang dikelola layanan Telegram ini |
| `/ringkasan paper` | Saldo, ekuitas, PnL, dan waktu valuasi simulasi |
| `/ringkasan live` atau `/saldo live` | Ringkasan akun Polymarket asli |
| `/posisi live 1` | Posisi akun real, 10 posisi per halaman; ganti angka untuk halaman berikutnya |
| `/posisi paper 1` | Posisi ledger simulasi |
| `/aktivitas live 1` | Transaksi yang tercatat di ledger live bot |
| `/aktivitas paper 1` | Transaksi simulasi; halaman tersedia untuk maksimum 50 transaksi terbaru |
| `/scan` atau `/scan paper` | Scan pasar non-crypto sekarang dengan aturan paper, tanpa order |
| `/scan live` | Scan publik dengan syarat forecast untuk live, tanpa order atau autentikasi akun |
| `/scan_last paper` atau `/scan_last live` | Laporan siklus terakhir pada ledger bot dan alasan penolakan |
| `/risiko` | Batas risiko dari `config.toml` |
| `/grafik live` atau `/grafik paper` | Gambar riwayat snapshot ekuitas |
| `/paper_once` | Jalankan satu siklus simulasi |
| `/paper_on` | Jalankan paper berulang sesuai `poll_seconds` |
| `/paper_off` | Minta proses paper milik Telegram berhenti |
| `/live_on` | Jalankan trading **uang asli** berulang |
| `/live_off` | Minta proses live milik Telegram berhenti |
| `/notifikasi_on` / `/notifikasi_off` | Aktif/nonaktifkan notifikasi transaksi; status proses dan kegagalan tetap diberitahukan |

Untuk mencari peluang saja, kirim `/scan` atau tekan tombol `/scan paper`. Bot mengirim tanda mulai lalu hasil scan baru: jumlah outcome, kandidat yang lolos, estimasi probabilitas model, anggaran/target harga, alasan penolakan, dan daftar pantauan yang **belum lolos entry**. Daftar pantauan hanya memakai orderbook segar yang lolos filter pasar, menampilkan alasan belum entry, serta minimum nilai order sebelum biaya/slippage. Harga pasar tidak diperlakukan sebagai probabilitas model. `/scan live` tetap membutuhkan forecast eksternal terkalibrasi. Keduanya menggunakan **modal analisis `starting_cash` dari konfigurasi**, tanpa posisi awal; hasilnya bukan pemeriksaan kelayakan saldo atau portofolio aktual. Scan tidak membuka posisi, menjalankan worker trading, ataupun mengubah ledger paper/live. File `STOP` tetap dihormati.

Ambang pada `config.toml` untuk entry paper/live kini dilonggarkan: likuiditas minimum $200 (sebelumnya $500), volume 24 jam $25 ($100), batas bawah probabilitas 70% (75%), confidence 70% (80%), dan edge 5% (8%). Maksimum analisis menjadi 120 outcome dengan anggaran 90 detik. Ini adalah ambang penyaringan, bukan bukti bahwa tingkat kemenangan menjadi 70%. Menurunkan ambang kualitas tidak menurunkan risiko kerugian. Entry $1, target profit minimum $0.50, validasi forecast, biaya, minimum order, data segar, dan batas modal tetap berlaku. Worker membaca konfigurasi saat mulai; setelah mengubahnya, hentikan worker dan jalankan kembali dari Telegram untuk menerapkannya.

Profil `scanner.toml` di sebelah `config.toml` dapat menimpa tujuh pengaturan penyaringan tersebut **hanya untuk scan manual**. Nilai profil saat ini sama dengan konfigurasi entry yang dilonggarkan. Ukuran posisi dan pengaman uang tidak dapat ditimpa lewat profil scanner. Hapus profil untuk membuat scan manual mengikuti seluruh nilai `config.toml`.

Jika kandidat lebih banyak daripada batas analisis, scan manual memakai sekitar sepertiga slot untuk pasar dengan waktu selesai terdekat dan menyebarkan sisanya ke seluruh horizon yang lolos filter. Ini mengurangi penumpukan pada pasar terdekat yang sama setiap kali `/scan` dijalankan. Siklus trading tetap memakai rotasi kandidat pada ledger.

Hanya satu permintaan data/scan diproses sekaligus, dengan jeda minimum 30 detik antarmulai scan. `/status` dan perintah berhenti tetap tersedia selama scan. Scan mengikuti `scan_pages` dari konfigurasi dasar serta batas analisis dari profil; proses dihentikan jika total waktu melebihi 3 menit. Batas ini berarti scan tidak mencakup seluruh pasar. Gangguan data dan hasil parsial diberi keterangan. Hasil scan dikirim walau notifikasi transaksi nonaktif; scan baru tidak mengganti laporan siklus trading dalam `/scan_last`. Setiap scan memakai profil terbaru saat proses scan dimulai.

Untuk simulasi entry, gunakan `/paper_once`, lalu baca `/scan_last paper`. Nol entry adalah hasil yang valid jika minimum order, forecast, data harga, atau batas risiko tidak terpenuhi. Hasil satu siklus tetap dikirim meskipun notifikasi transaksi nonaktif.

Konfigurasi saat ini mengaktifkan `paper_entry_mode = "explore"`: `/paper_once` dan `/paper_on` boleh membuka **maksimum satu entry eksplorasi paper per siklus** ketika forecast tidak tersedia. Hanya saldo simulasi yang dipakai. Ukuran entry dasar tetap $1; ukuran pertumbuhan tidak dipakai untuk eksplorasi. Crypto, minimum order, fee, bid/ask depth, data segar, cadangan kas, batas posisi dan kerugian tetap diperiksa. Perkiraan kerugian langsung dari spread/fee maksimum separuh batas stop-loss dan tidak lebih dari 10%. Kandidat yang mempunyai forecast tetapi gagal ambang bukti tidak diganti menjadi eksplorasi.

Eksplorasi ditandai pada `/scan paper`, posisi, dan aktivitas. Probabilitas serta edge ditampilkan sebagai belum tersedia. Target profit minimum $0.50 adalah tujuan exit mekanis, bukan forecast atau janji hasil. Horizon eksplorasi maksimum `paper_exploration_horizon_hours` (saat ini 6 jam), dibatasi penutupan pasar. `/scan` hanya menampilkan kandidat; entry terjadi melalui worker paper. `/scan live` dan `/live_on` tidak dapat memakai jalur eksplorasi. Untuk menonaktifkannya ubah `paper_entry_mode` menjadi `forecast_required` dan mulai ulang worker paper.

`/live_on` mengizinkan pengiriman order uang asli secara otomatis ketika strategi memenuhi semua syarat. Pemeriksaan saldo, allowance, geoblock, minimum order, rekonsiliasi ledger, batas kerugian, dan forecast eksternal tetap berlaku. Telegram tidak menyediakan forecast baru; kebutuhan forecast yang layak dijelaskan di [FORECASTS.md](FORECASTS.md). Kredensial Polymarket di `.env` diperlukan untuk akun real. Bot tidak melakukan deposit, penarikan, approval token, atau redemption otomatis.

Stop menahan pengiriman berikutnya dan memberi waktu bagi pekerjaan/order yang sudah dimulai untuk menyelesaikan pencatatan. Stop tidak membatalkan transaksi yang sudah terkirim, menjual posisi, atau menutup semua risiko akun. Perintah Telegram hanya mengontrol worker yang dimulainya sendiri; proses bot yang dijalankan manual memakai process lock yang sama dan tidak akan diambil alih. File darurat `data/STOP` tetap dihormati dan tidak dihapus melalui Telegram.

## Arti data pemantauan

- Ringkasan real membaca saldo dan posisi terbuka/tertutup yang dilaporkan Polymarket. PnL posisi terlapor bukan perhitungan seluruh arus kas wallet. Data parsial diberi catatan; nilai posisi bukan janji harga yang bisa dieksekusi.
- Aktivitas berasal dari ledger **bot** (maksimum 50 transaksi terbaru per mode), sehingga tidak mencakup seluruh transaksi manual akun. Posisi lama yang termasuk crypto dapat tampil pada akun real; strategi entry bot tetap mengecualikan crypto.
- Data paper terpisah dari uang asli. Valuasi posisi paper mengacu pada scan terakhir yang berhasil; waktu valuasi dicantumkan.
- Grafik hanya menggunakan snapshot yang benar-benar pernah dikumpulkan, terpisah menurut mode dan wallet. Riwayat sebelum pemantauan tidak dikarang. Deposit dan penarikan juga mengubah ekuitas. Riwayat disimpan maksimum 30 hari dengan pengambilan titik berjarak minimal 30 detik; kunjungi `/ringkasan` atau `/grafik` untuk menambah snapshot. Tidak ada grafik jika data belum tersedia.
- `/status` memeriksa worker milik layanan Telegram saat ini. Scan lama atau ledger yang tersedia tidak berarti bot masih berjalan. Setelah layanan restart, kirim `/paper_on` atau `/live_on` lagi; perintah lama yang antre tidak menjalankan trading otomatis.

## Proses background di Windows

Dari `D:\PolyBOT`:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\telegram.ps1 -Action Start
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\telegram.ps1 -Action Status
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\telegram.ps1 -Action Stop
```

Setelah layanan siap, VS Code dan terminal boleh ditutup. Komputer harus tetap menyala, tersambung internet, dan tidak sleep. Layanan berjalan sebagai pengguna Windows saat ini tanpa jendela baru. `Stop` mengirim permintaan berhenti melalui modul Python; tidak membunuh proses berdasarkan PID. Proses yang sedang menyelesaikan pekerjaan dapat memerlukan waktu sebelum benar-benar selesai.

Helper memakai `.venv\Scripts\python.exe`, `config.toml`, dan direktori proyek tempat helper berada. `state_dir` dari konfigurasi menentukan lokasi metadata/log (default `data`). `Status` memverifikasi PID, waktu pembuatan, executable, dan command line; hanya proses yang dimulai helper ini dilaporkan. Proses yang dibuka langsung melalui `python -m polybot.telegram` tidak dicatat helper, tetapi dapat dihentikan melalui `--stop`.

Log layanan yang telah disanitasi berada di `data/telegram.stdout.log` dan `data/telegram.stderr.log`. Saat mulai ulang, log sebelumnya dipindah ke akhiran `.previous`; log run sebelumnya lagi akan diganti. Metadata helper ada di `data/telegram-background.json`. Helper tidak mencetak `.env`, token, private key, atau raw exception SDK.

Startup otomatis bersifat opsional:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\telegram.ps1 -Action InstallStartup
```

Perintah ini mendaftarkan task untuk **saat pengguna Windows ini login**, bukan sebelum login. Task memakai hak pengguna biasa dan mencoba restart layanan yang gagal maksimum tiga kali dengan jeda satu menit. Ia memantau proses layanan, bukan menjamin keberhasilan koneksi Telegram atau trading. Kegagalan jaringan sementara juga dicoba ulang oleh aplikasi. Instalasi task tidak langsung memulai layanan. Setelah restart layanan, trading tetap menunggu perintah baru dari HP. Implementasi menggunakan [pengaturan resmi Windows Task Scheduler](https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/new-scheduledtasksettingsset).

Hapus startup dengan:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\telegram.ps1 -Action RemoveStartup
```

Menghapus task tidak menghentikan layanan yang sudah aktif; gunakan `Stop` juga bila diperlukan. Jika kebijakan Windows menolak pendaftaran task, layanan tetap dapat dijalankan memakai `Start`. Matikan sleep melalui pengaturan daya Windows bila komputer akan dipakai untuk pemantauan terus-menerus.

## Bila tidak merespons

1. Jalankan `--check`; jika koneksi gagal, periksa token secara lokal, internet, DNS, dan akses jaringan ke `api.telegram.org`. Jangan menempelkan token pada perintah diagnostik atau membagikan isi `.env`.
2. Pastikan pairing berhasil, lalu kirim pesan melalui chat **pribadi** pemilik. Grup, akun lain, pesan forward, dan perintah mulai yang terlalu lama tidak diterima.
3. Periksa `Status` dan log lokal. Konflik polling berarti ada instance lain memakai token yang sama. Gunakan satu layanan per token dan bot khusus tanpa webhook.
4. Bila data real belum tersedia, periksa `.\.venv\Scripts\python.exe -m polybot doctor --live`. Ini membaca akun tanpa membuat order. Laporan kegagalan Telegram hanya menampilkan pesan aman, bukan detail kredensial.

Untuk menjalankan layanan di terminal saat diagnosis:

```powershell
.\.venv\Scripts\python.exe -u -m polybot.telegram
```

Hentikan instance background dahulu agar tidak berebut polling. Ctrl+C atau `python -m polybot.telegram --stop` meminta layanan dan worker miliknya berhenti dengan aman.
