const fs=require('fs');
const p='services/hf-nvidia-nvidia-nemotron-nano-9b-v2/app/nexus_model_service.py';
fs.writeFileSync(p,fs.readFileSync('_tmp_p1.txt','utf8')+fs.readFileSync('_tmp_p2.txt','utf8')+fs.readFileSync('_tmp_p3.txt','utf8'));
console.log('done');