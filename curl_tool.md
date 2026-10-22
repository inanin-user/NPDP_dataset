## This is a curl tool to get response from NPDP or NPS application

### Admin account for these two application
username: xxxxxxx
password: xxxxxxx

### Link to NPDP application
URL: 18.167.5.207/production/


### Link to NPS application
URL: 18.166.169.171/test/

### Rule for replace content wrapped by backquote(`) in practice
1. `link` should be application link
2. `area` should be specified by the LLM
2. `function` should be specified by the LLM
### Login steps through curl
1. open terminal in the host
2. use command: curl -i -L `link` -c cookies.txt -d "username=`username`&password=`password`&submit=Login"
3. use command: curl -i -L `link`/Default.php -b cookies.txt

### Access function after login
1. open terminal in the host
2. use command: curl -i -L `link`/`area`/`function` -b cookies.txt
3. return the response to the LLM