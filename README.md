# odb2kicad

DesignSpark PCB が書き出した ODB++ を、KiCad 9 の基板ファイル(`.kicad_pcb`)に変換する。

Converts ODB++ exported from DesignSpark PCB into a KiCad 9 board file (`.kicad_pcb`), including footprints (pads only), nets, tracks, vias and filled copper pours. Pure Python, no dependencies. See below (in Japanese) for details and limitations.

## 何ができるか

DesignSpark PCB の無料版(Explorer)で作った基板を、KiCad で開いて調べられるようにする。
KiCad は ODB++ の書き出しはできるが、読み込みはできない(KiCad 10 の時点)。Gerber からの取り込みでは部品とネットが失われるが、この変換では残る。

移すもの:

| ODB++ | KiCad |
|---|---|
| 部品(参照番号、部品名) | フットプリント(パッドだけ) |
| パッド(丸、四角、角丸、長丸、回転した四角などの独自の形) | パッド(表面実装、貫通、メッキなしの穴) |
| ネット | ネット |
| 配線、円弧 | 配線、円弧の配線 |
| ビア | ビア |
| ベタ面 | 塗りつぶし済みのゾーン(元の形のまま) |
| シルク、図面の層 | 線、円弧、多角形 |
| 基板の外形 | Edge.Cuts |
| 部品に属さないパッド、取り付け穴 | 1パッドのフットプリント |

## 使い方

Python 3 が要る(3.14 で確かめた)。ほかのパッケージは要らない。

1. DesignSpark PCB で基板を開き、「Output」→「ODB++..」を選び、「OK」で書き出す(`.tgz` ができる)。
2. 変換する。

```bash
python3 odb2kicad.py board.tgz board.kicad_pcb
```

展開済みの ODB++ のフォルダ(`matrix/` がある所、またはその上のフォルダ)を渡してもよい。

3. KiCad の PCB エディタで `board.kicad_pcb` を開く。

## 注意

- **ゾーンを塗り直さない。** ベタ面は元の形をそのまま写してある。KiCad で「すべてのゾーンを塗りつぶす」(B キー)を押すと、KiCad の設計ルールで塗り直されて元の形と変わる。
- **設計ルールは入らない。** クリアランスなどは KiCad の既定値になる。元の設計の間隔が既定値(0.2mm)より狭いと、DRC でクリアランスの違反が多く出る。「基板の設定」で、元の設計に合わせて直す。
- **層構成(厚さ、誘電率)は入らない。** KiCad の既定(1.6mm、FR4)になる。インピーダンスなどを評価するときは、実際の値に直す。
- **フットプリントはパッドだけ。** 部品の外形の線、ライブラリとの対応、3D モデルは無い。
- **回路図は移らない。** ODB++ には回路図が入っていない。
- インチ単位の ODB++ だけに対応している(DesignSpark PCB はインチで書き出す)。
- 基板は、KiCad の図面の上で (100 mm, 150 mm) を原点にして置く。

### ネットの推定

DesignSpark PCB は、ベタ面と、サーマルの細い配線などの一部の配線を、ネット無し(`$NONE$`)として書き出す。そのままでは、GND のベタ面がどのネットにもつながらない。
そこで、次の手がかりからネットを推定して付ける。

1. ベタ面の中にある、ネットの分かったパッドの中心と配線の端の多数決。
2. 配線の端が、ネットの分かったベタ面の中にあるか、ネットの分かったパッド(実際の形で判定する)や配線に触れていれば、そのネット。
3. 1と2を、変化が無くなるまで繰り返す。中に何も無いベタ面は、縁に触れている配線とパッドから決める。
4. どの名前付きのネットにも届かない銅箔は、つながっている同士をまとめて `ODB_NoNet_<番号>` という仮のネットにする。

推定の結果は、変換の最後に表示される(`net_inferred_*`、`net_inference_conflicts` など)。`net_inference_conflicts` が 0 でないときは、別々のネットの手がかりがぶつかった箇所があるので、KiCad の DRC で確かめる。

## 確かめたこと

DesignSpark PCB で作られた、公開されている4枚の基板(4層が3枚、2層が1枚)で確かめた。

- **形:** 同じ `.pcb` から DesignSpark PCB が書き出した Gerber と、変換した基板から KiCad が書き出した Gerber を、`gerber_compare.py` で 0.02mm の格子の画像にして重ねた。銅箔の全14層で、1画素(0.02mm)を超える違いは無かった。
- **つながり:** KiCad の DRC で、4枚ともショートは無かった。3枚では、未接続は元の設計で接続しないピン(`nc` のネットの2ピン)だけだった。
  残りの1枚では、GND の2か所の未接続と、どのネットにもつながらないベタ面5か所(`ODB_NoNet_*`)が残った。元のデータでネットが無い部分で、元の設計でも同じかは確かめられていない。

## gerber_compare.py

2つの Gerber を同じ格子の画像にして重ね、違いを数える。変換の確認に使う。原点の違いは、画像の相互相関で自動で合わせる。
numpy と Pillow が要る。

```bash
python3 gerber_compare.py original.gbr converted.gbr diff.png
```

違いの画像では、元だけにある所を赤、変換後だけにある所を青で示す。

## ライセンス

MIT License(`LICENSE`)。
