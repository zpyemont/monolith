Step 1; Strimzi
1.. add the repo: 
    helm repo add strimzi https://strimzi.io/charts/
2. install version 0.45.0 (latest that supports zookeeper) 
helm install strimzi-kafka-operator \
  strimzi/strimzi-kafka-operator \
  --version 0.45.0
3. create kafka topics: 
    kubectl apply -f ./kubernetes/kafka/kafka-topics.yaml
4. create kafka and zookeoper cluster: 
    kubectl apply -f ./kubernetes/kafka/kafka-cluster.yaml



Step 2; Flink
1. add the bitnami flink repo: 
    helm repo add bitnami https://charts.bitnami.com/bitnami
2. create flink jobmanager and taskmanager: 
    helm install my-flink bitnami/flink \
  --namespace flink --create-namespace \
  --set image.registry=docker.io \
  --set image.repository=bitnami/flink \
  --set image.tag=1.20.1-debian-12-r4


Step 3; Monolith
1. Install the Training Operator control plane:
    kubectl apply --server-side -k "github.com/kubeflow/training-operator.git/manifests/overlays/standalone?ref=v1.8.1"
2. Deploy the Monolith Batch training job:
    kubectl apply -f ./kubernetes/monolith/batch.yaml
3. create the kafka user:
    kubetl apply -f ./kubernetes/monolith/kafka-user.yaml
4. create the monolith-secret:

kubectl get secret my-cluster-cluster-ca-cert \                                                                     
  -o go-template='{{index .data "ca.crt" | base64decode}}' \                
  > ca.crt

kubectl get secret my-cluster-cluster-ca \                
  -o jsonpath='{.data.ca\.key}' \
  | base64 --decode \                        
  > ca.key

openssl genpkey -algorithm RSA \                          
  -out client.key \
  -pkeyopt rsa_keygen_bits:2048     

openssl req -new \                                        
  -key client.key \
  -subj "/CN=monolith-zk-client/O=monolith" \
  -out client.csr

openssl x509 -req \                                       
  -in client.csr \
  -CA ca.crt \
  -CAkey ca.key \
  -CAcreateserial \
  -out client.crt \
  -days 365 \
  -extfile zk-client-ext.cnf \
  -extensions v3_req

openssl verify -CAfile ca.crt client.crt  
kubectl create secret generic monolith-zk-client \        
  --from-file=ca.crt=./ca.crt \
  --from-file=client.crt=./client.crt \
  --from-file=client.key=./client.key


4. Deploy the Monolith Online training job:
    kubectl apply -f ./kubernetes/monolith/online.yaml



Extra: 

Minikube use local docker image: 

eval $(minikube docker-env)
docker build -t monolith .

Model loading:
https://chatgpt.com/c/67f44635-e5a4-8007-b898-60ce3ce100c1

Connections between online and serving:
https://chatgpt.com/c/67f6ed07-4b4c-8007-a649-e4a10e8168f4


