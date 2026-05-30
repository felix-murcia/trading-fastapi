FROM n8nio/n8n:latest

# Cambiamos al usuario correcto
USER node

# Creamos carpeta para nodos personalizados
RUN mkdir -p /home/node/.n8n/custom

# Instalamos nodos adicionales dentro de la carpeta custom
RUN cd /home/node/.n8n/custom && \
    npm install @n8n/n8n-nodes-langchain

# Volvemos al directorio de trabajo por defecto
WORKDIR /home/node

# El entrypoint original de n8n se mantiene intacto
