# Use a lightweight Python image as the base
FROM python:3.12

# Set the working directory inside the container
WORKDIR /src

# Copy your dependencies file and install them
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code into the container
COPY . .

# Command to run the bot
CMD ["python", "main.py"]
