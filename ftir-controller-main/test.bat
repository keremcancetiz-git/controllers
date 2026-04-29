curl.exe http://localhost:8080/health

curl.exe http://localhost:8080/experiments/experiment-folders

curl.exe -X POST http://localhost:8080/omnic/collect-bkg -H "Content-Type: application/json" -d "{\"experiment_name\":\"test\"}"

curl.exe -X POST http://localhost:8080/omnic/collect-sample -H "Content-Type: application/json" -d "{\"experiment_name\":\"test\"}"

pause