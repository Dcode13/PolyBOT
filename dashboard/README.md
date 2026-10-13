# PolyBOT Dashboard

Dashboard lokal Next.js 16 + React 19 + Tailwind CSS 4 untuk akun Polymarket dan bot Python di folder induk. Font tersimpan lokal; tidak memerlukan CDN.

## Menjalankan

Node.js 20.9+ dan environment Python PolyBOT diperlukan. Dependensi sudah dipasang pada workspace ini.

```powershell
cd D:\PolyBOT\dashboard
npm run dev
```

Buka **http://127.0.0.1:3000**. Untuk build produksi:

```powershell
npm run build
npm run start
```

Jika memakai checkout baru, jalankan `npm ci` di folder dashboard dan siapkan environment Python serta SDK live mengikuti README utama. Server hanya mendengarkan `127.0.0.1`; tidak dipublikasikan ke internet. Hentikan dengan Ctrl+C. Jangan mengubah host menjadi `0.0.0.0` tanpa menambahkan autentikasi dan peninjauan akses.

## Yang tersedia

- Ringkasan cash, nilai posisi, ekuitas, PnL posisi, dan order terbuka.
- Pemantauan akun asli dan ledger paper melalui pilihan sumber data.
- Snapshot grafik ekuitas, filter waktu, tooltip mouse/keyboard, dan pilihan menyembunyikan nilai.
- Pencarian/filter/urutan posisi, dialog detail, tautan pasar, serta ekspor CSV.
- Aktivitas transaksi bot dari ledger lokal, ringkasan scan, dan konfigurasi risiko.
- Pembaruan otomatis 30/60/120 detik atau manual; preferensi tersimpan di browser.
- Tampilan responsif desktop dan ponsel.

## Sumber data dan batas interpretasi

`GET /api/monitor?mode=live|paper` menjalankan modul `polybot.monitor` melalui Python pada sisi server. Perintah dan parameter bersifat tetap; input browser tidak menjadi perintah shell. Respons hanya berisi field yang diseleksi untuk tampilan.

Untuk akun asli, backend memakai SDK Polymarket membaca cash, order terbuka, holdings termasuk yang diarsipkan, dan riwayat posisi tertutup. PnL merupakan agregat **posisi yang terlapor oleh bursa**, bukan perhitungan keuntungan berdasarkan semua deposit/withdrawal. Sisa shares kecil dapat tetap memiliki nilai terlapor walaupun statusnya ditutup. Nilai ini bukan jaminan harga atau kemampuan untuk menjualnya.

Tabel menampilkan posisi lama termasuk crypto yang pernah ada pada akun; hal ini tidak mengubah filter entry bot. Posisi `REDEEMABLE` dapat bernilai nol jika kalah. Riwayat pada tab Aktivitas hanya transaksi bot yang tersimpan di ledger; tidak mengklaim memuat seluruh aktivitas manual akun.

Grafik menggunakan snapshot nyata yang mulai dicatat saat pemantauan dijalankan, bukan rekonstruksi riwayat ekuitas sebelum dashboard dibuat. Sampling disimpan terpisah pada `data/monitor.sqlite3`, minimal 30 detik antar-snapshot per mode/wallet, retensi 30 hari, maksimal 10.000 catatan per identitas. Grafik menerima hingga 200 titik representatif. Snapshot gagal atau valuasi tidak lengkap tidak menghasilkan titik nol buatan. Grafik live dan paper terpisah.

Label scan terbaru hanya menandakan adanya hasil scan lokal yang masih baru; bukan bukti bahwa proses bot sedang berjalan. Dashboard tidak menjalankan scanner atau engine trading.

## Kredensial dan akses

Gunakan `.env` pada **folder induk PolyBOT**, bukan `.env` di dashboard dan bukan variabel `NEXT_PUBLIC_*`. Private key dibaca hanya oleh proses Python dan tidak disertakan ke respons API, HTML, JavaScript browser, atau browser storage. File ledger trading dibuka secara read-only; satu-satunya penulisan backend monitor adalah penyimpanan snapshot pemantauan.

Tidak ada endpoint untuk buy/sell, withdrawal, deposit, redemption, atau pengubahan strategi. Autentikasi SDK dapat membuat/mengambil kredensial API akun. Server memeriksa host dan origin lokal, menolak permintaan lintas situs, dan memberi respons API `no-store`. Cache server 20 detik mengurangi permintaan bursa yang berulang.

## Verifikasi

```powershell
npm run typecheck
npm test
npm run build
```

Uji browser memakai Chrome yang sudah terpasang. Jalankan server dahulu, kemudian pada terminal lain:

```powershell
npx playwright test
```

Uji browser membaca akun dan membuat screenshot lokal di `test-results` yang diabaikan Git. Screenshot dan CSV dapat memuat data akun; jangan dibagikan tanpa memeriksa isinya. Uji tidak membuat order. Jika Chrome belum tersedia, pengujian browser memerlukan browser yang sesuai konfigurasi Playwright.
