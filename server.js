const express = require('express');
const multer = require('multer');
const path = require('path');
const fs = require('fs');

const app = express();
const upload = multer({ dest: 'uploads/' });

app.post('/api/upload', upload.single('pdf'), (req, res) => {
  const pdfPath = req.file.path;

  // Tu podłącz parser PDF → MT940
  const mt940Content = '...wygenerowany plik MT940...';

  const outputPath = path.join(__dirname, 'outputs', `${req.file.filename}.mt940`);
  fs.writeFileSync(outputPath, mt940Content);

  res.json({
    success: true,
    downloadUrl: `/downloads/${req.file.filename}.mt940`
  });
});

app.use('/downloads', express.static(path.join(__dirname, 'outputs')));

app.listen(3000, () => {
  console.log('Serwer działa na http://localhost:3000');
});
