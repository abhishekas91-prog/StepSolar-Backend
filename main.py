from fastapi import FastAPI
app=FastAPI()
@app.get('/api/health')
def health(): return {'status':'ok'}
@app.post('/api/leads')
def leads(data:dict): return {'success':True,'data':data}
