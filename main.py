import socket

from HTTP.http_resolver import HTTPResolver

server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

server.bind(("127.0.0.1", 8080))
server.listen(5)

print("Server running at http://127.0.0.1:8080")

while True:
    client_socket, client_address = server.accept()

    request = client_socket.recv(4096).decode("utf-8")

    request = HTTPResolver().resolve(request)
    # print(request)
    print(HTTPResolver.response_dict(request[0], "200", "OK", request[3], "body"))
    body = "Hello HTTP Server!"

    response = (
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {len(body.encode('utf-8'))}\r\n"
        "\r\n"
        f"{body}"
    )

    client_socket.sendall(response.encode("utf-8"))
    client_socket.close()