# Forecast dan batas kemampuan analisis

Bot mempunyai dua sumber analisis: eksperimen pola harga dan forecast independen dari file lokal. Keduanya dapat salah. Tidak ada harga, nilai `confidence`, atau label `calibrated` yang membuktikan suatu posisi pasti menang.

Bot tidak otomatis mengetahui kekuatan tim, cuaca aktual, hasil pemilu, atau berita baru hanya dari riwayat harga. Penelitian fundamental tersebut harus dilakukan produsen forecast independen. Memasang bot saja belum menyediakan produsen ini. Forecast eksperimen tidak memenuhi syarat untuk order live; mode live membutuhkan forecast eksternal yang sesuai persyaratan bot dan aktivasi live oleh pengguna.

## Memilih Yes atau No

Bot dapat membuka posisi pada kedua outcome. Forecast `resolution` untuk **Yes** menilai kemungkinan peristiwa terjadi; forecast untuk **No** menilai kemungkinan peristiwa tidak terjadi, sesuai aturan penyelesaian pasar. Masing-masing memakai token, orderbook, biaya, dan forecast miliknya sendiri. Outcome tidak diasumsikan dari urutan token; pasar dengan nama outcome seperti nama tim tetap memakai nama yang diberikan bursa.

| Tindakan | Order ke bursa | Arti |
| --- | --- | --- |
| Entry Yes | `BUY` token Yes | Membeli saham Yes |
| Entry No | `BUY` token No | Membeli saham No |
| Exit Yes | `SELL` token Yes yang dimiliki | Menjual posisi Yes |
| Exit No | `SELL` token No yang dimiliki | Menjual posisi No |

Entry No bukan order `SELL` atau membuka short. Budget, target profit, stop-loss, dan pemeriksaan entry tetap sama. Bot hanya boleh memegang satu posisi per event sehingga tidak membuka Yes dan No sekaligus pada event yang sama.

Untuk mendukung No melalui feed eksternal, produsen perlu menulis record dengan `market_id` pasar tersebut dan **`token_id` No yang tepat**. `probability`, `lower_probability`, dan `target_price` harus menjelaskan saham No itu. Menambah teks `outcome: "No"` pada record yang masih memakai token Yes tidak mengalihkan forecast ke No.

Bot tidak membalik forecast Yes menjadi No secara otomatis. Untuk peristiwa settlement biner yang saling melengkapi, `1 - lower_probability(Yes)` merupakan **batas atas** peluang No, bukan batas bawah konservatif. Sementara itu, kejadian harga Yes menyentuh target dan harga No menyentuh target tidak saling melengkapi: keduanya bisa terjadi pada waktu berbeda dalam satu horizon. Karena itu forecast `target_touch` juga harus dibuat khusus untuk token dan target yang dimaksud.

Eksperimen riwayat harga dan eksplorasi paper dapat memilih No, tetapi pilihan tersebut tidak menyatakan bahwa bot telah membuktikan peristiwa tidak akan terjadi. Eksplorasi paper tetap tidak mempunyai estimasi probabilitas hasil.

## Mengapa target $0,50 dari modal $1 tidak selalu tersedia

Jika satu saham dibeli seharga $0,90, modal $1 memperoleh sekitar 1,111 saham sebelum biaya. Bahkan bila setiap saham akhirnya membayar $1, keuntungan kotornya hanya sekitar $0,111. Probabilitas menang tinggi tidak otomatis menyediakan keuntungan besar.

Keuntungan $0,50 dari modal $1 berarti imbal hasil 50%. Harga saham perlu naik sekitar 50% sebelum biaya. Spread, biaya transaksi, jumlah minimum saham, dan kedalaman orderbook membuat kebutuhan sebenarnya lebih besar. Bot dapat melewatkan semua pasar apabila target ini tidak didukung. Tidak melakukan transaksi adalah hasil yang sah.

Harga pernah menyentuh target juga belum berarti semua saham bisa dijual pada harga tersebut. Untuk transaksi, yang relevan adalah pembeli yang tersedia, harga bid yang dapat dieksekusi, ukuran posisi, dan biaya.

## Eksperimen riwayat harga

`QuantitativeForecaster` mencari harga sekarang yang berada di bawah median harga sebelumnya. Targetnya adalah kemungkinan pemulihan harga menuju median tersebut. Ini hipotesis *mean reversion*, bukan penilaian probabilitas hasil pertandingan atau peristiwa.

Aturan validasinya:

- Data harus berurutan, finite, cukup baru, dan tidak memiliki jeda besar. Harga masa depan tidak ikut perhitungan.
- Minimal 96 observasi diperlukan secara default; jumlah ini sendiri belum cukup untuk menghasilkan forecast.
- Bagian awal 60% menjadi periode pelatihan, dan 40% terakhir menjadi periode evaluasi terpisah.
- Setiap target historis hanya memakai harga sebelum awal prediksi tersebut.
- Jendela evaluasi tidak saling tumpang tindih dan harus mempunyai satu horizon pengamatan penuh.
- Default membutuhkan minimal 3 peristiwa pembanding pada pelatihan dan 8 pada evaluasi. Horizon default 24 jam.
- `target_touch_probability` adalah frekuensi target tersentuh pada periode evaluasi; `target_touch_lower_bound` adalah batas bawah Wilson 95% deskriptif. `confidence` memakai nilai batas bawah tersebut.

Nilai Wilson mengasumsikan kondisi statistik yang belum tentu berlaku pada pasar. Jendela yang tidak tumpang tindih tetap dapat saling berkaitan; kondisi pasar bisa berubah. Pemilihan peristiwa pembanding, perubahan likuiditas, dan keterbatasan data juga memengaruhi hasil. Karena itu nilai ini tidak boleh dibaca sebagai tingkat keyakinan terverifikasi atas keuntungan transaksi berikutnya.

Hasil eksperimen selalu memiliki `calibrated: false` dan `forecast_type: "experimental"`. Tidak ada `probability` atau `lower_probability` fundamental. Statistik pelatihan dan evaluasi berada di objek `validation`. Biaya, spread, dan kedalaman orderbook diperiksa terpisah oleh strategi.

Pasar baru atau pasar berumur pendek sering tidak mempunyai cukup bukti historis. Tidak ada fallback yang mengubah harga terakhir menjadi probabilitas kemenangan.

## Eksplorasi paper tanpa forecast

`paper_entry_mode = "explore"` mengizinkan percobaan simulasi ketika kedua sumber forecast belum menghasilkan sinyal. Ini bukan sumber forecast tambahan: rencana diberi `entry_kind = "paper_exploratory"`, `probability_lower = null`, `confidence = null`, dan `edge = null`. Kandidat diurutkan menurut biaya eksekusi bolak-balik saat ini, setelah kandidat dengan forecast. Target exit dihitung dari tujuan profit konfigurasi; tidak ada klaim bahwa harga akan mencapainya.

Semua pemeriksaan data, minimum order, depth, biaya, dan batas portofolio tetap berlaku. Maksimum satu percobaan entry eksplorasi dilakukan per siklus, memakai ukuran entry dasar. Forecast yang tersedia tetapi gagal syarat bukti tetap ditolak. Mode live tidak menerima rencana eksplorasi, termasuk pada batas fungsi eksekusi. Kembalikan `paper_entry_mode` ke `forecast_required` untuk paper yang selalu memerlukan forecast.

## Feed forecast eksternal

`ExternalForecasts` hanya membaca file JSON lokal. Format berikut adalah **contoh yang sengaja kedaluwarsa, dengan ID placeholder dan nilai bukti nol**. Contoh ini tidak dapat dipakai sebagai sinyal transaksi.

```json
{
  "forecasts": [
    {
      "market_id": "PLACEHOLDER_MARKET_ID_BUKAN_PASAR_NYATA",
      "token_id": "PLACEHOLDER_TOKEN_ID_BUKAN_TOKEN_NYATA",
      "probability_kind": "target_touch",
      "probability": 0.0,
      "lower_probability": 0.0,
      "target_price": 0.60,
      "confidence": 0.0,
      "horizon_hours": 24,
      "observed_at": "2000-01-01T00:00:00Z",
      "expires_at": "2000-01-02T00:00:00Z",
      "source": "CONTOH_SKEMA_BUKAN_FORECAST",
      "rationale": "Contoh struktur saja; tidak memiliki data atau bukti untuk transaksi.",
      "calibrated": false
    }
  ]
}
```

Jangan mengubah tanggal dan `calibrated` semata-mata agar contoh lolos. Produsen harus membuat forecast baru dengan identitas pasar yang tepat, data aktual, metode yang dapat diperiksa, dan bukti evaluasi.

| Field | Arti dan validasi |
| --- | --- |
| `market_id`, `token_id` | String identitas pasar dan saham outcome yang harus cocok persis. Probabilitas YES tidak boleh dipakai sebagai probabilitas NO. |
| `probability_kind` | `resolution` atau `target_touch`; bila tidak ada, default `resolution`. Sebaiknya selalu tulis secara eksplisit. |
| `probability` | Estimasi probabilitas sesuai jenisnya, dalam rentang 0–1. |
| `lower_probability` | Batas bawah konservatif yang diberikan produsen, antara 0 dan `probability`. Bot memvalidasi angkanya, bukan membuktikan validitas statistiknya. |
| `target_price` | Target harga saham, lebih dari 0 dan kurang dari 1. Bukan janji harga jual atau keuntungan. |
| `confidence` | Ukuran mutu bukti dari produsen, dalam rentang 0–1. Tidak boleh diisi tinggi tanpa metode yang jelas. |
| `horizon_hours` | Horizon sejak `observed_at`, antara 0,01 dan 8760 jam. Batas strategi bisa lebih ketat. |
| `observed_at` | Waktu pembuatan/pengamatan forecast dalam ISO-8601 dengan zona waktu. Timestamp masa depan tidak dipakai. |
| `expires_at` | Waktu kedaluwarsa eksplisit, harus sesudah `observed_at`. |
| `source` | Sumber/model/produsen yang dapat ditelusuri; teks wajib. |
| `rationale` | Penjelasan bukti dan alasan forecast; teks wajib. |
| `calibrated` | Boolean; default `false`. Nilai `true` hanyalah pernyataan produsen, bukan sertifikasi bot. |

`resolution` berarti probabilitas saham outcome membayar ketika pasar diselesaikan. Nilai ini tidak menyatakan kemungkinan harga naik ke `target_price` hari ini. Nilai harapan saat penyelesaian juga berbeda dengan nilai harapan strategi yang menjual lebih awal atau menjalankan stop-loss.

`target_touch` berarti probabilitas harga menyentuh `target_price` selama horizon sejak `observed_at`. Probabilitas itu harus berasal dari model khusus pergerakan harga dalam horizon tersebut. Forecast yang makin tua tidak otomatis mempunyai probabilitas yang sama untuk sisa waktunya; produsen perlu memperbaruinya ketika keadaan berubah. Kemampuan mengeksekusi penjualan tetap memerlukan analisis terpisah.

Reader menghentikan penggunaan forecast pada batas paling awal: `expires_at`, akhir horizon, atau usia maksimum reader—default 24 jam. Field keluaran `remaining_horizon_hours` berisi sisa horizon sejak waktu pembacaan; horizon awal tidak boleh dimulai kembali setiap kali bot membaca file. Strategi dan batas waktu pasar dapat memperpendek waktu transaksi lebih lanjut.

Seluruh feed ditolak bila ada record cacat, nama properti JSON ganda, atau `token_id` ganda, termasuk token yang muncul pada pasar berbeda. NaN, Infinity, angka berupa string, boolean sebagai angka, timestamp tanpa zona waktu, sumber kosong, dan batas bawah lebih tinggi daripada probabilitas ditolak. Ukuran file dibatasi 1 MiB. File dibaca kembali sehingga pembaruan produsen dapat berlaku tanpa restart.

## Apa yang perlu dibuktikan produsen

Kalibrasi membutuhkan riwayat prediksi yang disimpan sebelum hasil diketahui, kemudian dibandingkan dengan kejadian yang benar-benar diamati. Pisahkan data pengembangan dan pengujian menurut waktu. Definisikan peristiwa yang diukur dengan jelas: kemenangan outcome berbeda dengan target harga tersentuh dalam 24 jam.

Periksa jumlah sampel, kesalahan probabilitas, kelompok probabilitas yang terlalu percaya diri, perubahan kualitas menurut kategori/horizon, serta biaya dan kegagalan eksekusi. Catat model, sumber data, waktu penerbitan, aturan pemilihan pasar, dan seluruh prediksi termasuk yang gagal. Menghapus prediksi buruk atau mengukur hanya transaksi yang dipilih setelah hasil diketahui membuat evaluasi menyesatkan.

Bot tidak melakukan audit independen terhadap bukti tersebut. Mengisi `calibrated: true` melewati pemeriksaan deklarasi, bukan membuktikan mutu. Lakukan evaluasi paper dengan bukti independen sebelum menggunakan dana live; hasil paper maupun keuntungan masa lalu tidak memastikan hasil berikutnya.
