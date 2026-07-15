# Is Zip Mein Kya Fix Kiya Gaya Hai

## ✅ 1. `main.py` — Config Path Fix (asal masla)

**Pehle:** Terminal se chalane par aur `.exe` se chalane par app **do alag
`config.json` files** use kar rahi thi — isi wajah se exe mein purana/khali
data dikh raha tha jabke terminal mein sab kuch sahi tha.

**Ab:** App hamesha ek hi fixed jagah use karti hai, chahe terminal se
chalao ya `.exe` se:

```
C:\Users\<AapKaUsername>\AppData\Local\FileMaker-AI-Chatbot\config.json
```

Is se dono modes hamesha same data share karenge.

## ✅ 2. `config.example.json` — Daily quota limit

`daily_request_limit` ko `20` se bara kar ke `50` kar diya hai, taake sir
ke sath demo/testing ke dauran app khud hi "quota reached" na bole.

## ✅ 3. Security — asal credentials is zip mein NAHI hain

Tumhari purani `config.json` (jisme real FileMaker password aur Gemini
key thi) is zip mein **jaan-boojh kar shamil nahi ki gayi**. Uski jagah
sirf `config.example.json` (khali template) hai. Pehli baar app chalne
par, ye template khud copy ho kar naya `config.json` ban jayega
(`AppData` wali jagah par).

**Zaroori:** Chunke tumhari purani key/password is chat mein share ho
chuki thi, unhe ek dafa **change/regenerate** kar lena — ehtiyatan.

---

# ⚠️ Zaroori: Ye khud add karna — `static/` folder

Mere paas tumhari `Fm_ai_chatbot_multi_db.rar` file ke andar wala
`static/` folder (jisme `index.html`, `chat.js`, `style.css` hain — yani
poora frontend/UI) extract karne ka tareeqa nahi tha (bina internet ke
`.rar` files nahi khul sakti).

**Isliye is zip mein `static/` folder shamil NAHI hai.** Ye karo:

1. Apni `Fm_ai_chatbot_multi_db.rar` (ya jahan bhi asal `static/` folder
   hai) se `static` folder ko copy karo
2. Isay is naye extract-ki-hui folder (`FM-AI-Chatbot`) ke andar paste
   kar do, `main.py` ke sath waali level pe — is tarah:
   ```
   FM-AI-Chatbot/
   ├── main.py
   ├── config.example.json
   ├── static/          <-- ye tum khud daloge
   │   ├── index.html
   │   ├── chat.js
   │   └── style.css
   ├── build_exe.bat
   └── ...
   ```

`static/` folder mein koi change nahi kiya gaya — usay chuye baghair,
jaisa hai waisa hi copy karna hai.

---

# Ab Rebuild Kaise Karo

```
1. Purani "venv", "build", "dist" folders is naye folder mein NAHI honge - 
   pehle setup_and_run.bat ek dafa chalao (venv banane ke liye):
       setup_and_run.bat

2. Us se app terminal mode mein khul jayegi - band kar do (Ctrl+C).

3. Phir .exe banane ke liye:
       build_exe.bat

4. Test karo:
       dist\FileMaker-AI-Chatbot.exe
```

Naya `.exe` bante hi wahi `AppData` wali fixed config use karega jaisa
terminal mode karta hai — dono hamesha sync mein rahenge.
